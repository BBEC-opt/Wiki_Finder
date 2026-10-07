# 后端设计

## 目标与边界

后端提供本地优先的文档摄取、解析、切分、混合检索和流式问答能力。设计借鉴 WeKnora 的核心链路，但保持单进程 FastAPI、SQLite 和本地文件存储，不依赖参考仓库。

## 模块职责

```text
app/main.py             应用装配、生命周期和异常处理
app/api/                HTTP/SSE 路由与 Pydantic 契约
app/core/               配置和通用错误
app/services/           摄取、切分、检索、RAG 业务流程
app/parsing/            文件格式探测、解析与标准化
app/infrastructure/     SQLite、仓储、文件存储、模型提供方
app/domain.py           解析过程使用的领域数据对象
```

依赖方向为 `api → services → infrastructure`。`main.py` 负责组装实例，基础设施不得反向依赖 API，路由不得实现解析、检索等核心算法。

## 主要数据流

### 文档摄取

1. API 接收文件或手工 Markdown。
2. `IngestionService` 保存源文件并创建知识与持久化任务。
3. `IngestionWorker` 执行解析、切分、Embedding、索引写入和 Wiki 页面生成。
4. `ParsingService` 输出 `parsed.md`、`manifest.json` 与可选图片。
5. `Repository` 原子替换 Chunk 与 FTS 索引，再把生成结果合并到知识库级 `wiki_pages` 并刷新来源关系。

任务状态为 `queued → running → succeeded/failed`；知识状态为 `pending → processing → completed/failed`。处理阶段依次为 `parsing → chunking → embedding → indexing → wiki_generation`，Wiki 成功写入后知识才进入 `completed`。进程启动时恢复排队任务和租约超时任务。

### Wiki 生成

Wiki 与解析预览分离。`parsed.md` 是忠实保留页码、表格和图片引用的中间产物；`WikiGenerationService` 从解析产物与 Chunk 生成文档概览、主题和原文入口，并合并进知识库级页面空间。`wiki_pages` 的 slug 在知识库内唯一，`wiki_page_sources` 记录页面到文档、Chunk 和页码的证据关系，`wiki_folders` 保存独立目录。离线模式使用确定性抽取保证链路完整；模型增强不得改变引用接地规则。

模型模式采用与 WeKnora 一致的 Map-Reduce 思路：

1. `candidate_extraction`：分批读取文档 Chunk，抽取实体和概念候选；跨批按规范化标题聚合，并优先保留被多个片段重复识别的对象，单批模型失败不丢弃其他批次结果；`focused` 使用更小的程序侧候选预算；
2. `citation_mapping`：候选必须携带有效 `chunk_indices`，无证据候选直接丢弃；
3. `deduplication`：以 NFKC、大小写和标点归一化标题合并同批候选并复用已有规范页，人工页优先；模型仅补充判断别名与明确同义，遵守“相关不等于相同”；
4. `taxonomy`：为整批候选规划最多两级目录；
5. `reduce`：按候选聚合证据与已有页面内容，生成严格接地的摘要和 Markdown 正文；
6. `finalize`：重建 `index/home`、Wiki 内链与反向链接，并提供统计和死链/无来源检查。

模型步骤使用严格 JSON 协议并校验 slug、页面类型和 Chunk 索引。任一模型步骤格式错误或调用失败时退回确定性生成，失败不会留下半写入页面。`wiki_generation` 阶段输出记录运行模式、候选数、引用 Chunk 数和已执行阶段，供离线回归与后续可观测平台采集。

自动页面标记为 `pipeline`，人工保存后标记为 `user`。文档重新处理只刷新来源关系和自动页面，不能覆盖人工正文；共享页面按文档保存独立 contribution。重解析或删除来源时，自动页必须由剩余 contribution 重建，禁止保留已撤销来源的正文；没有其他来源的纯自动页面才会清理，随后重建索引、内链与反向链接。

### 检索与问答

切分采用显式父子结构：`parent_chunks` 保存按标题、页码和 `parent_chunk_size` 聚合的有界父块，`chunks.parent_id` 指向父块；只有子块生成 Embedding 和 FTS 索引。`RetrievalService` 在指定知识范围内对孩子执行 Dense 与 SQLite FTS5/BM25 检索，再用 RRF 融合并去重，命中后通过父块扩展 `context_content`。旧数据的 `chunks.parent_content` 仅作为迁移兼容回退，新入库不再重复写父内容。

**检索 Pipeline（与 WeKnora 对齐）**：
1. **召回阶段**：Dense（向量）+ Sparse（BM25/FTS5）并行检索
2. **RRF 融合**：`fusion_score = vector_weight/(k+vector_rank) + keyword_weight/(k+keyword_rank)`，k、权重可配置（默认 k=60，权重 1:1）
3. **去重**：基于 `content_hash` 去重
4. **Reranker 重排序**（可选）：对候选结果应用深度语义模型重排序
   - 离线模式：基于 jieba 分词的启发式排序
   - 本地模式：FlagEmbedding（BGE-reranker-v2-m3）
   - 复合评分：`0.6*rerank_score + 0.3*rrf_score + 0.1*position_prior`
   - 阈值过滤（默认 0.3），保留最高分作为回退（最低 0.15）
5. **MMR 多样性**（可选）：应用 Maximal Marginal Relevance 减少结果冗余
   - 基于 Jaccard 相似度计算文本重叠
   - Lambda 参数平衡相关性与多样性（默认 0.7）
   - 增量算法：每轮只计算新选中项与剩余候选的相似度
6. **截断与编号**：返回 Top-K 结果

Reranker 通过 `app/infrastructure/rerank_provider.py` 管理，支持离线与本地模型两种模式，模型加载失败自动回退到离线实现。MMR 集成在 `RetrievalService._apply_mmr`，使用 jieba 分词和 Jaccard 相似度。配置项包括 `RERANK_ENABLED`、`RERANK_MODEL`、`RERANK_THRESHOLD`、`RRF_K`、`VECTOR_WEIGHT`、`KEYWORD_WEIGHT`、`MMR_ENABLED`、`MMR_LAMBDA`。

RagService 先校验并固定会话的知识库及资料范围，再从已完成轮次选取最多三轮历史。真实模型把追问改写成独立检索问题，15 秒超时或失败时回退原问题；离线模式仅拼接最近问题作为检索上下文，不承诺模型级指代理解。继续复用现有 RetrievalService，不变更向量模型或索引。

历史使用不超过 max_context_chars 的三分之一（最多 4000 字符），按完整轮次选取；证据使用扣除历史与当前问题后的剩余预算，首条超长证据也裁剪，序号重新从 1 编排。资料以转义后的 XML 数据块传给模型，不执行资料中的指令；无证据时使用确定性资料不足回答，不调用远端生成模型。

SSE 依次发送 start、references、answer、done，生成失败发送 error。start 携带 session_id、turn_id、message_id、request_id；answer 保留 delta；done 增加 status（completed/stopped），error 使用固定提示和 answer_failed 错误码，不回显内部异常。每 15 秒发送心跳注释。消息 ID 在生成前分配，不随重试变化。

## 存储与配置

- SQLite 保存知识库、知识、任务、阶段、Chunk 和聊天记录。
- SQLite 的 `wiki_pages` 保存知识库级页面，`wiki_page_sources` 保存证据，`wiki_folders` 保存目录；初始化时兼容迁移旧的单文档页面表。
- `data/uploads/` 保存源文件，`data/artifacts/` 保存解析产物；均为运行时数据。
- 无 API Key 时使用离线 Provider；配置 API Key 后使用 OpenAI-compatible 接口。默认真实模型示例采用阿里云百炼北京地域、`qwen-plus` 和 1024 维 `text-embedding-v4`。该模型同步接口单批最多 10 条，Provider 会将配置批大小自动收敛到模型上限，并显式传递 `dimensions`；错误响应保留服务端错误体但不记录请求头或密钥。
- `WIKI_EXTRACTION_GRANULARITY` 控制 `focused / standard / exhaustive` 抽取密度，默认 `standard`；`WIKI_MAX_CANDIDATES` 限制单篇文档候选数，默认 12。
- Embedding 模型或维度变化后，已有知识必须重新处理或新建知识库。

## API 与错误约定

- API 前缀为 `/api/v1`，普通成功响应统一为 `{ "success": true, "data": ... }`。
- 普通错误统一为 `{ "success": false, "error": { "code", "message", "details" } }`。
- 流式问答在建立 SSE 响应后的错误通过 `error` 事件表达。
- 输入在 Pydantic 模型或服务边界校验，文件下载必须限制在对应产物目录内。
- Wiki 页面使用 `GET /knowledge-bases/{kb_id}/wiki/pages` 列出，支持 `knowledge_id` 与 `q` 过滤；知识库子资源提供页面 CRUD、slug 查询、来源和目录接口。旧的 `GET /wiki/pages/{page_id}` 暂时保留兼容。
- `GET /knowledge-bases/{kb_id}/wiki/stats` 返回页面、链接和来源统计；`GET /knowledge-bases/{kb_id}/wiki/lint` 返回死链与无来源页面问题。

## 设计决策

- 当前规模采用单仓储类和进程内 Worker，避免引入消息队列与多层接口。
- API、业务流程、基础设施按目录隔离，但不为每个类创建单独协议层。
- Dense 检索采用 SQLite JSON 加进程内计算，适用于演示和小数据集；替换向量库时保持服务层调用边界稳定。
- Wiki 生成是索引后的独立阶段；自动页面与人工页面共享知识库级命名空间，但采用不同覆盖策略。

## Langfuse 可观测性

- `app/infrastructure/tracing.py` 管理可选 SDK 4.16.0、内容策略、追踪生命周期与错误隔离；Web 与独立 Worker 分别创建客户端，退出时在线程中调用 shutdown，等待批量导出。
- `LANGFUSE_ENABLED` 默认 false，不导入 SDK、不发网络请求；启用需安装 `.[langfuse]` 并配置 `LANGFUSE_BASE_URL/PUBLIC_KEY/SECRET_KEY`。地址必须显式指定，密钥用 SecretStr 保存。环境由 `LANGFUSE_TRACING_ENVIRONMENT` 指定，默认 development。
- 追踪层级：`rag-answer → hybrid-search → embedding` 与 `rag-answer → generation`；`document-ingestion → embedding / wiki-generation → Wiki 子步骤 → generation`。直接检索自成根追踪。问答传播 session_id；摄取每次执行单独追踪并记录 task_id、knowledge_id 和尝试次数，不跨队列传播请求上下文。
- 保留现有 httpx Provider，使用手动 observation 和 ContextVar 维护父子关系。每次推进异步生成器后恢复调用方上下文，避免 SSE 暂停期间串线；异常、取消与显式关闭均结束 observation。业务内部捕获的错误显式标记 ERROR。
- 默认仅写入统计和标识符，异常仅记录类型；`LANGFUSE_CAPTURE_CONTENT=true` 才记录问答、模型消息及检索正文，开启后这些数据将发送到配置的 Langfuse。完整上传文件、认证头和配置对象不进入追踪。SDK 操作失败输出固定告警，不回显异常内容、不改变业务结果。
- 真实模型记录模型名及服务端 usage；开启追踪时对流式聊天请求加 `stream_options.include_usage`，兼容服务需支持该参数，usage-only 空 choices 事件正常处理。无 usage 不伪造计数；离线模型标记为 hash-embedding / extractive-offline。
- API/SSE 与持久化契约无变化，不需要迁移或重新生成向量。自动测试使用真实 SDK 的内存 exporter，不访问 Langfuse 或付费模型；远端凭据就绪后还需上传示例资料并发起问答，在 Langfuse 审核层级、会话和用量。

## 会话持久化与生命周期

- chat_sessions 保存 id、title、knowledge_base_id（可空）、knowledge_ids_json、active_turn 和时间戳。一个会话固定一个知识库及资料集合；空集合表示全库。单会话跨知识库请求返回 422，已绑定范围不一致返回 409。
- chat_messages 增加 turn_id 和 status；user 与 assistant 通过 turn_id 关联。回答状态为 generating → completed/failed/stopped，停止请求先记为 stopping。失败和未完成轮次不会进入后续模型历史。
- Repository.begin_chat 在事务中条件更新 active_turn 并创建消息，确保同一会话仅一个生成请求；修改/清空/删除活跃会话返回 409。重试只允许最后一条 failed/stopped 回答，核对原问题后复用消息 ID 和问题记录，分配新 turn_id。
- 生成任务与 SSE 消费协作运行，最多缓冲 32 个事件。每秒保存部分正文、引用并续期；每 250 毫秒检查停止状态。停止、客户端断连和生成器关闭都会取消上游并在屏蔽取消的作用域内保存最终状态。进程异常退出时，超过两分钟没有续期的轮次在读取历史/会话列表或再次发送时恢复为 stopped，保留最近一次快照。
- SQLite 和 PostgreSQL 初始化均增量迁移消息字段，并为已有 session_id 补建“旧会话”。旧消息不删除、不猜测知识库；缺少范围的会话仅供查看、重命名、清空或删除。迁移幂等，不修改 Embedding 或已有知识，无需重新入库。
- 引用持久化保留回答时的片段快照及 Chunk/Wiki 标识；资料删除、重处理后旧回答不会变成无来源空白，前端明确提示原始来源已失效。

### 会话 API

| 方法与路径（前缀 /api/v1） | 契约 |
|---|---|
| POST /sessions | knowledge_base_id、可选 knowledge_ids/title；201 返回会话 |
| GET /sessions?knowledge_base_id=… | 当前知识库会话，按更新时间倒序；legacy=true 单独列出范围未知的旧会话 |
| PATCH /sessions/{id} | title；更新会话标题 |
| DELETE /sessions/{id} | 原子删除会话与消息 |
| GET /sessions/{id}/messages | 保留 data 数组；limit 默认 100、范围 1–100，before 为消息 ID，返回页内正序记录 |
| DELETE /sessions/{id}/messages | 保留旧接口，只清空消息 |
| POST /sessions/{id}/stop | 请求停止，返回 data.stopped=true；最终状态通过 SSE 或历史确认 |
| POST /chat | 保留旧字段，新增可选 retry_message_id；首次使用未知 session_id 时自动创建并绑定单知识库范围 |

消息响应增补 turn_id/status，保留 references_json 字段以兼容旧消费者。旧 /chat 单知识库请求仍有效；旧客户端多知识库请求不再接受，需分别创建会话。建立流之前进行输入/范围/并发校验，之后的错误通过 SSE 返回。会话接口与现有系统一样面向本地单用户，不提供身份或租户隔离。

追踪继续使用当前固定的 Langfuse SDK 4.16.0 和内容采集开关，新增问答逻辑不要求升级 SDK 或接入外部服务。取消和改写均沿用现有模型 Provider；自动测试使用本地假模型验证改写、失败、取消和引用约束。
