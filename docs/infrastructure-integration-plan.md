# Redis、Qdrant、PostgreSQL 与 MinIO 接入优化方案

## 1. 目标与结论

本方案基于当前项目代码与本机 `WeKnora-main` 的只读分析，目标是把现有单机 SQLite、本地文件和进程内任务队列演进为可水平扩展的部署，同时保留开发环境的低门槛。

推荐的最终职责边界：

| 组件 | 唯一职责 | 是否事实源 | 当前替代对象 |
| --- | --- | --- | --- |
| PostgreSQL | 业务元数据、任务状态、Wiki、Chunk 正文、事务与 outbox | 是 | SQLite |
| Qdrant | Dense 向量及过滤 payload，执行向量候选召回 | 否，可由 PostgreSQL 重建 | SQLite JSON 向量和进程内余弦计算 |
| MinIO | 原文件、解析 Markdown、manifest、图片等二进制产物 | 是（文件内容） | `data/uploads/`、`data/artifacts/` |
| Redis | 任务通知、短期进度、分布式锁、限流和可选缓存 | 否 | `asyncio.Queue` 与进程内协调状态 |

不建议同时把 PostgreSQL 的全文检索也迁入 Qdrant。Qdrant 的主优势是向量检索和 payload 过滤；中文关键词相关性仍由 PostgreSQL 全文方案提供，再沿用当前 RRF 融合。第一版可先使用 PostgreSQL `tsvector`；若中文召回质量不达标，再评估 ParadeDB/BM25 或应用侧分词后的 `tsvector`，不要在首轮迁移中增加额外数据库。

## 2. 当前项目基线与瓶颈

当前实现具备清晰的 `api → services → infrastructure` 边界，但基础设施能力集中在单进程：

- `app/infrastructure/database.py` 用 `aiosqlite` 初始化 13 类业务表和 FTS5；缺少正式版本化迁移工具。
- `app/infrastructure/repository.py` 是单一大仓储，SQL 使用 SQLite 占位符、FTS5 语法和 SQLite 特有异常文本。
- `app/services/retrieval.py` 从 SQLite 拉取候选 Chunk，解析 JSON 向量后逐条计算余弦；数据量增长后 CPU、内存和延迟近似线性增长。
- `app/services/ingestion.py` 通过单进程 `asyncio.Queue` 驱动一个 Worker；重启恢复依赖数据库扫描，无法安全扩展多个 API/Worker 实例。
- `app/infrastructure/storage.py` 直接返回本地 `Path`，解析器和下载路由依赖本机目录；多实例之间无法共享文件。
- 索引替换在 SQLite 内可一次完成；拆到 PostgreSQL 与 Qdrant 后将失去跨库事务，需要显式的一致性协议。

## 3. 从 WeKnora 借鉴与不照搬的设计

### 3.1 借鉴

- 检索引擎提供稳定能力边界，向量与关键词独立召回，上层统一做 RRF；Qdrant 按维度隔离 collection，并使用 payload 做知识库、文档过滤。
- PostgreSQL 同时承担业务数据和默认检索时，可获得较好的事务一致性；独立 Qdrant 适合向量规模和吞吐需要单独扩容的场景。
- 文件存储与向量存储分离；对象记录持有后端标识和对象键，而不是持有只在当前机器有效的绝对路径。
- Redis 队列仅负责投递，任务真相、待处理操作和死信仍持久化在数据库；任务具备确定性 ID、去重键和幂等处理。
- 存储后端切换必须显式迁移，不能只改配置后让旧对象失联；连接测试要做真实的最小读写验证。
- 外部后端启动失败需要健康状态、超时、冷却和恢复机制，不能让每个请求都重复长时间试连。

### 3.2 暂不照搬

- 当前项目没有租户和多存储实例需求，不引入 WeKnora 的租户级注册表、动态密钥配置和管理 UI。
- 不在第一阶段支持多种向量库或 S3 厂商；只保留小而稳定的内部协议以及 `local/minio`、`memory/redis` 两种运行模式。
- 不做多向量库双写常态化。双写只用于迁移窗口，完成后 Qdrant 是唯一 Dense 索引。

## 4. 目标架构与数据所有权

```text
HTTP/SSE API
  ├─ PostgreSQL：业务事务、任务状态、Chunk 正文、关键词索引、outbox
  ├─ MinIO：source/{kb_id}/{knowledge_id}/... 与 artifact/{knowledge_id}/...
  ├─ Redis：task-ready 通知、进度 pub/sub、短租约锁、限流
  └─ Worker
       ├─ 从 PostgreSQL claim 任务
       ├─ 从 MinIO 下载到 data/tmp/{task_id}/ 后解析
       ├─ 写 PostgreSQL Chunk/父块/关键词索引
       └─ 消费 outbox，幂等 upsert/delete Qdrant point

查询：PostgreSQL 关键词 Top-N ─┐
                               ├─ RRF、去重、父块扩展、引用返回
      Qdrant 向量 Top-N ───────┘
```

核心原则：

1. PostgreSQL 中的 Chunk ID、内容、状态和 outbox 决定系统事实；Qdrant payload 不保存完整正文，只保存返回过滤所需的 ID、版本、知识库 ID、知识 ID、启用状态和内容哈希。
2. Qdrant point ID 直接使用稳定的 Chunk UUID。所有 upsert/delete 都携带 `index_version`，重复消费不产生重复数据。
3. MinIO 数据库字段保存 `storage_backend`、`bucket`、`object_key`、`etag`、`size`，禁止保存预签名 URL；URL 按请求短期生成。
4. Redis 中不保存唯一副本。Redis 清空后，Worker 可从 PostgreSQL `queued` 任务和未完成 outbox 重新投递。
5. 删除顺序采用“数据库标记删除/停用 → outbox 删除外部索引和对象 → 成功后清理记录”，避免外部删除成功但事务回滚。

## 5. 模块改造设计

### 5.1 PostgreSQL

建议引入 SQLAlchemy 2 async + `asyncpg` 和 Alembic。不要机械兼容两套 SQL；把 SQLite 定位为测试/轻量模式，把 PostgreSQL 定位为生产模式，并对差异明确实现。

- 将 `Database` 改为连接池生命周期管理，设置连接获取、语句和事务超时；API 与 Worker 分配独立池上限。
- 拆分当前 `Repository` 的稳定职责：业务仓储、任务仓储、检索索引仓储。无需为每张表创建类。
- ID 优先使用应用生成 UUID，时间统一为 UTC `timestamptz`，可枚举状态使用 `CHECK` 约束。
- `chunks.embedding` 在 Qdrant 上线后移除；迁移窗口可保留 nullable JSONB，但不得长期双份存储。
- 为常用路径建立组合索引：任务 `(status, available_at)`、Chunk `(knowledge_base_id, knowledge_id, enabled)`、Wiki slug 唯一约束及现有来源/链接查询索引。
- Worker claim 使用 `SELECT ... FOR UPDATE SKIP LOCKED`，以数据库租约保证多 Worker 不重复执行。
- 新增 `outbox_events(id, aggregate_type, aggregate_id, event_type, payload, status, attempts, available_at, locked_until, created_at, processed_at, last_error)`；业务写入与 outbox 写入同一事务。
- 全文检索用存储的 `tsvector` 和 GIN；中文内容沿用当前 `jieba` 预分词结果，避免直接依赖 PostgreSQL 默认中文切词能力。

### 5.2 Qdrant

- 新增小型 `VectorIndex` 协议：`ensure_collection`、`upsert_chunks`、`search`、`delete_by_knowledge`、`healthcheck`；服务层不接触 Qdrant SDK 类型。
- collection 建议命名为 `chunks_v1_{dimension}`，距离使用 Cosine。按维度分 collection，避免不同知识库模型维度冲突；payload 用 `knowledge_base_id` 和 `knowledge_id` 建 keyword 索引。
- 批量 upsert，限制批大小和请求字节数；为 RPC 设置连接、请求和总体检索超时，重试仅覆盖明确的瞬态错误并带抖动退避。
- 查询向 Qdrant 下推 KB、knowledge、enabled 和版本过滤，并使用 `score_threshold`；仅取 `candidate_k` 个 ID 和分数，再到 PostgreSQL 批量 hydration 正文与父块。
- Qdrant 不承担关键词检索。当前 `RetrievalService` 的 RRF 输出字段和 API 契约保持不变。
- 提供重建命令：按 PostgreSQL Chunk 游标读取 → 重新计算或读取迁移期向量 → 写新 collection → 校验数量/抽样召回 → 原子切换 collection alias。禁止原地破坏性重建。
- Embedding 模型或维度变化视为新索引版本；完成重建前旧 alias 继续服务。

### 5.3 MinIO

- 将 `LocalStorage` 抽象为面向对象键的 `ObjectStorage`，至少包含流式写入、流式读取、下载到临时文件、列举文档对象和按前缀删除；本地实现继续作为默认离线模式。
- 上传时边流式计算 SHA-256 边写临时对象，创建数据库记录成功后提交为最终对象键；失败清理临时对象。大文件使用 SDK multipart，不把内容整体读入内存。
- Parser 仍接收本地 `Path`：Worker 把对象下载到 `data/tmp/{task_id}/`，解析产物再上传 MinIO；任务结束可靠清理临时目录。这能保持解析模块最小改动。
- 下载 API 由服务端鉴权后流式代理，或生成短期预签名 URL；解析图片等受保护资源不能直接暴露永久公网 URL。
- bucket 默认私有，启动时只检查 bucket 和权限，不自动修改已有 bucket 策略。凭据只能来自环境或 Secret Manager，不落日志和 API 响应。
- 建议对象键不含用户原始路径：`source/{kb_id}/{knowledge_id}/{safe_name}`、`artifact/{knowledge_id}/{build_id}/parsed.md|manifest.json|images/...`。
- 数据库提交与对象写入无法原子化：增加 `object_state=pending/ready/deleting/error` 或对象 outbox，并提供定期孤儿对象清理任务。

### 5.4 Redis

- 第一用途是唤醒 Worker，不替代 PostgreSQL 任务表。生产可用 Redis Streams consumer group；若选择轻量 list，也必须保留数据库 claim 和恢复扫描。
- 消息体只放 `task_id`、类型、trace ID 和 schema version，不放完整 Markdown、Embedding 或密钥；消费前必须从 PostgreSQL claim。
- ACK 必须发生在数据库状态和必要 outbox 已提交之后。消费者崩溃后通过 pending reclaim 与任务租约恢复。
- 短期进度使用 `progress:{task_id}`，设置 TTL；最终状态始终读取 PostgreSQL。SSE 可订阅 pub/sub，但断线重连先读 PostgreSQL 快照。
- 分布式锁仅用于定时恢复扫描、索引 alias 切换等必须单执行者的操作；锁值使用随机 owner token，并实现 compare-and-delete 与续租。
- 配置 ACL 用户、TLS、连接池和命名空间前缀；不同环境、测试和应用不能共享裸 key。
- Redis 不可用时：API 仍可把任务写入 PostgreSQL并返回 queued；后台轮询器继续消费，性能降低但不丢任务。

## 6. 分阶段实施计划

### 阶段 0：边界与可观测基线

改动：

- 为数据库、对象存储、向量索引和任务通知定义最小内部协议；保持路由和服务契约不变。
- 增加结构化指标：摄取各阶段耗时、队列深度、任务重试、Qdrant 延迟、PostgreSQL 池等待、MinIO 吞吐和错误率。
- 固化 3 组基准数据（小中文文档、大 PDF、多知识库过滤）与当前召回结果。

验收：现有 SQLite/local/asyncio 模式全量测试通过；API 响应无变化。

### 阶段 1：PostgreSQL 事实源

改动：

- 引入 Alembic，建立 PostgreSQL schema 和索引；迁移脚本把 SQLite 按依赖顺序导入并校验行数、外键和内容哈希。
- 改造事务、占位符、时间类型、唯一冲突判断和 FTS 查询；实现 `SKIP LOCKED` claim。
- 新增 outbox，但此阶段可先由本地 dispatcher 消费。

切换：短暂停写 → 最终增量导入 → 校验 → 切换 `DATABASE_URL`。保留 SQLite 只读快照用于回滚，不做长期双写。

验收：所有 API/端到端测试在 PostgreSQL 跑通；并发 claim 不重复；迁移前后业务表计数、关键哈希一致。

### 阶段 2：MinIO 对象存储

改动：

- 先引入 `ObjectStorage` 并让 local 实现通过原测试，再增加 MinIO 实现与临时文件桥接。
- 增加对象元数据和状态；编写本地文件到 MinIO 的可重入迁移工具，先复制校验后改数据库引用。

切换：读取支持 local/MinIO 两种定位；新写先切 MinIO；历史对象后台复制并校验 SHA-256；确认无旧引用后才允许人工清理本地文件。

验收：上传、解析、预览、图片、重处理、删除均通过；中断迁移可续跑；越权对象键和路径穿越测试通过。

### 阶段 3：Qdrant Dense 索引

改动：

- 引入 `VectorIndex`、Qdrant adapter 和 outbox dispatcher；索引阶段在 PostgreSQL 原子提交 Chunk 与 upsert 事件。
- 检索改为 Qdrant ID 召回 + PostgreSQL hydration，并继续和 PostgreSQL 关键词结果 RRF。
- 实现 collection alias、全量重建、对账和孤儿 point 清理。

切换：影子写入 Qdrant → 离线对账 → 影子查询比较 Top-K 重合率与延迟 → 按配置启用 Qdrant 读取 → 停止 JSON 向量写入。

验收：过滤无跨 KB 泄漏；重复事件幂等；删除/重处理后无旧 point；维度不匹配在入库前失败；P95 达到约定目标。

### 阶段 4：Redis 与独立 Worker

改动：

- 把 Worker 从 FastAPI 生命周期拆为独立进程；Redis 只投递 task ID，PostgreSQL claim 和租约决定所有权。
- 增加 pending reclaim、数据库兜底扫描、进度 TTL、死信查看与人工重试命令。

切换：先单 Worker 使用 Redis，再逐步增加并发；依据模型限流、数据库池和解析资源设置并发，不直接按 CPU 数放大。

验收：API、Worker 任意一方重启不丢任务；重复消息不重复生成数据；Redis 清空/短时不可用后任务最终完成；多 Worker 压测无重复 claim。

### 阶段 5：收敛与清理

- 删除 SQLite JSON 向量、只服务迁移的双读双写代码和无引用配置。
- 保留 `local + SQLite + asyncio` 作为明确的 lite profile，或正式宣布只支持生产 profile；不要维持行为不明确的自动降级。
- 更新 `README.md`、`.env.example`、`docs/backend-design.md` 和运维手册；提供备份恢复、重建索引、轮换凭据和容量告警流程。

## 7. 配置建议

配置按组件分组，敏感值不得给出可用默认值：

```dotenv
APP_PROFILE=lite
DATABASE_URL=postgresql+asyncpg://user:password@postgres:5432/my_wiki

OBJECT_STORAGE_DRIVER=local
MINIO_ENDPOINT=minio:9000
MINIO_ACCESS_KEY=
MINIO_SECRET_KEY=
MINIO_BUCKET=my-wiki
MINIO_SECURE=false

VECTOR_INDEX_DRIVER=memory
QDRANT_URL=http://qdrant:6333
QDRANT_API_KEY=
QDRANT_COLLECTION_PREFIX=my_wiki

TASK_NOTIFIER_DRIVER=memory
REDIS_URL=redis://redis:6379/0
REDIS_PREFIX=my_wiki:dev
```

`lite` 默认保持零外部依赖；`production` 启动时 PostgreSQL 必须可用，Qdrant/MinIO 按已启用能力执行 fail-fast，Redis 可降级为 PostgreSQL 轮询。配置日志只输出驱动、主机和能力状态，不输出 DSN 密码、API Key 或 Secret Key。

## 8. 一致性、失败与回滚矩阵

| 场景 | 预期行为 | 修复来源 |
| --- | --- | --- |
| PostgreSQL 不可用 | 停止接受写入；健康检查失败 | 数据库恢复/主从切换 |
| Qdrant 不可用 | 入库业务事务可提交，outbox 重试；检索降级为关键词并明确标记 | PostgreSQL Chunk + outbox |
| MinIO 不可用 | 文件类上传返回可重试错误；已落库 pending 对象可清理 | PostgreSQL 对象状态 |
| Redis 不可用 | 任务仍入 PostgreSQL，Worker 轮询；实时进度能力下降 | PostgreSQL 任务表 |
| Worker 中途退出 | 租约过期后重新 claim；各阶段幂等 | PostgreSQL 任务/阶段状态 |
| Qdrant 数据损坏 | 建新 collection 并切 alias | PostgreSQL Chunk + Embedding Provider |
| 对象删除失败 | 保持 deleting 状态并后台重试 | PostgreSQL 对象 outbox |

任何阶段回滚只回滚流量开关，不删除新后端数据。旧数据至少保留一个确认周期；涉及文件和 SQLite 快照的清理必须由运维显式执行。

## 9. 测试与验收清单

- 单元：各 adapter 契约、对象键安全、Qdrant payload/filter、Redis 消息版本、outbox 幂等和退避。
- 集成：用固定版本容器运行 PostgreSQL、Qdrant、Redis、MinIO；每个测试使用独立数据库/schema、collection、bucket prefix 和 Redis prefix。
- 故障注入：请求超时、连接断开、重复消息、ACK 前崩溃、索引写成功但状态更新失败、MinIO multipart 中断。
- 迁移：空库、存量库、中断续跑、重复执行、哈希不一致、旧数据回滚。
- 检索质量：固定查询集记录 Recall@K、MRR、Top-K 重合率；分别覆盖中文、英文、混合文本和 KB/文档过滤。
- 性能：上传吞吐、摄取任务吞吐、10/100/1000 并发检索 P50/P95/P99、数据库连接池等待和 Qdrant 批写吞吐。
- 安全：凭据脱敏、私有 bucket、预签名 URL TTL、路径穿越、SSRF/非法 endpoint、跨知识库过滤和删除权限。

建议上线门槛：迁移计数与哈希 100% 一致；故障恢复测试无任务丢失；跨 KB 泄漏为 0；影子检索质量不低于当前基线；连续观察一个发布周期后再清理旧存储。

## 10. 依赖、部署与运维建议

- 固定容器大版本和 Python 客户端版本，不使用 `latest`；开发环境用 Compose profile 按需启动，CI 使用精简集成测试服务。
- PostgreSQL 每日全量备份并做 PITR；MinIO 开启 versioning/lifecycle（若容量允许）；Qdrant 以可重建为主，可结合规模决定是否做 snapshot；Redis 不作为备份依赖。
- 健康检查分 `live` 与 `ready`：进程存活不要求所有依赖正常；生产写请求 readiness 至少要求 PostgreSQL 和启用中的 MinIO 正常。
- 监控 outbox backlog、最老事件年龄、任务租约超时、Redis pending、Qdrant point 对账差异、MinIO pending/deleting 对象和数据库池耗尽。
- 容量优先按 Chunk 数、平均向量维度、原文件/产物字节数和每日任务峰值测算；不要仅按文档数量估算。

## 11. 推荐执行优先级

1. PostgreSQL：先建立事实源、迁移体系、并发任务 claim 与 outbox，是其余三项可靠接入的前提。
2. MinIO：改动边界相对独立，可尽早解除多实例共享文件的限制。
3. Qdrant：在 outbox 和对象 ID 稳定后上线，避免把一致性问题写进业务服务。
4. Redis：最后拆 Worker；此时任务状态已在 PostgreSQL 中可靠持久化，Redis 才能保持“加速而非真相”的定位。

该顺序比一次性替换四个组件更容易验证和回滚，也避免为当前规模提前引入 WeKnora 的多租户、多实例注册和复杂管理面。
