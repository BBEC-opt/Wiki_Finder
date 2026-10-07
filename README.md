# Wiki_Finder

这是一个用 Python 实现的、可独立运行的知识入库与 RAG 问答示例。它保留了 WeKnora 核心链路，同时用 SQLite、本地文件和进程内可恢复 Worker 减少部署依赖。

## 已实现

- 知识库、知识、Chunk、Wiki 页面、任务、阶段和会话持久化
- SQLite 持久化任务队列，重启后恢复 queued/超时 running 任务
- 文件与手工 Markdown 异步入库、SHA-256 去重、重处理和删除
- 格式探测与 Parser Registry
- TXT、Markdown、HTML、MHTML、PDF、DOCX、XLSX、CSV、PPTX 和图片解析
- DOCX/PPTX/PDF 图片提取，解析产物 `parsed.md + manifest.json + images/`
- PDF 页码保留、扫描页检测和可选 Tesseract OCR
- 标题/页码感知切片，代码块、公式、Markdown 表格保护
- 离线 Hash Embedding 与 OpenAI-compatible Embedding
- 中文分词、SQLite FTS5/BM25、Dense 检索和 RRF 融合
- Reranker 模型重排序与 MMR 多样性算法（与 WeKnora 对齐）
- 带引用的 Prompt、离线摘取式回答与 OpenAI-compatible 流式回答
- SSE `references → answer → done/error`
- 独立 Wiki 生成阶段，提供概览、主题目录、来源追溯和原文页
- 同源响应式 Web 工作台，支持知识库、资料与流式问答操作

## 快速启动

```powershell
python -m pip install -e .
uvicorn app.main:app --reload
```

访问 Web 工作台：<http://127.0.0.1:8000/>  
访问 Swagger：<http://127.0.0.1:8000/docs>。如果使用 `scripts/start.ps1`，脚本默认端口为 `8001`。

默认是完全离线模式，不需要 API Key。

## 项目结构

```text
app/
├─ main.py             # 应用入口与依赖装配
├─ api/                # HTTP/SSE 路由与数据契约
├─ core/               # 配置与通用错误
├─ services/           # 摄取、切分、检索与 RAG
├─ parsing/            # 文档解析
└─ infrastructure/     # 数据库、仓储、存储与模型适配
frontend/              # 原生 Web 工作台
docs/                  # 前端与后端设计文档
tests/                 # 自动化测试
samples/               # 示例资料
scripts/               # 本地启停脚本
data/                  # 数据、日志、缓存与运行状态（不提交）
```

设计边界分别见 `docs/frontend-design.md` 和 `docs/backend-design.md`。

## 完整演示

创建知识库：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/knowledge-bases \
  -H "Content-Type: application/json" \
  -d '{"name":"产品知识库","description":"Demo"}'
```

上传文档（把 `{kb_id}` 替换成上一步返回值）：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/knowledge-bases/{kb_id}/knowledge/file \
  -F "file=@samples/product_guide.md"
```

上传接口返回 `pending`。通过下面接口观察 `pending → processing → completed`：

```bash
curl http://127.0.0.1:8000/api/v1/knowledge/{knowledge_id}
curl http://127.0.0.1:8000/api/v1/knowledge/{knowledge_id}/stages
curl http://127.0.0.1:8000/api/v1/knowledge/{knowledge_id}/artifacts
```

混合检索：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query":"如何恢复出厂设置？","knowledge_base_ids":["{kb_id}"],"top_k":5}'
```

流式问答：

```bash
curl -N -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id":"demo","query":"如何恢复出厂设置？","knowledge_base_ids":["{kb_id}"]}'
```

## 使用真实模型

复制 `.env.example` 为 `.env`：

```env
MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MODEL_API_KEY=your-dashscope-api-key
CHAT_MODEL=qwen-plus
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSION=1024
EMBEDDING_BATCH_SIZE=10
PARENT_CHUNK_SIZE=3200
MODEL_TIMEOUT_SECONDS=120
EMBEDDING_BATCH_SIZE=20
```

默认示例使用阿里云百炼北京地域的 OpenAI-compatible 接口；其他兼容服务也可以指向对应网关。切换 Embedding 模型或维度后，应新建知识库或重处理全部知识。

## Langfuse 追踪（可选）

安装接入依赖（Python SDK 4.16.0）：

```powershell
python -m pip install -e ".[langfuse]"
```

在本地 `.env` 配置 Langfuse 项目地址和 API Keys，然后重启 Web 服务及独立 Worker：

```env
LANGFUSE_ENABLED=true
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk-lf-你的公钥
LANGFUSE_SECRET_KEY=sk-lf-你的密钥
LANGFUSE_TRACING_ENVIRONMENT=development
LANGFUSE_CAPTURE_CONTENT=false
```

地址应与项目地域或自建服务一致；密钥在 Langfuse 项目的 Settings → API Keys 创建，不提交到 Git。关闭时无需 SDK、密钥或网络，保持默认离线能力。

上传一份示例资料，再发起检索和问答，可以在 Langfuse 查看 `document-ingestion`、`hybrid-search`、`rag-answer`；Wiki 子步骤和模型调用嵌套在对应流程下，会话通过 `session_id` 聚合。独立 Worker 的每次处理尝试各自生成追踪，通过 task_id 和 knowledge_id 关联。

默认采集 ID、数量、模型名、耗时、错误类型及模型返回的 Token 用量，不发送文档、提示词和回答正文。确需排查内容时设置 `LANGFUSE_CAPTURE_CONTENT=true`，此时问答、模型消息和检索正文会传至所配服务。离线模型及未返回 usage 的兼容模型不估算 Token 或费用。启用追踪时流式模型需支持 `stream_options.include_usage`。

追踪故障不改变业务结果；进程正常退出时等待 SDK 导出。该接入不改 API/SSE、数据库或向量维度，无需数据迁移或重新入库。参考 [Langfuse SDK 文档](https://langfuse.com/docs/observability/sdk/instrumentation)。

## Reranker 与 MMR（可选）

系统已集成 **Reranker 模型重排序** 和 **MMR 多样性算法**，显著提升检索质量：

```powershell
# 安装 Reranker 依赖（可选，推荐）
python -m pip install -e ".[rerank]"
```

在 `.env` 中启用：

```env
RERANK_ENABLED=true
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_THRESHOLD=0.3
MMR_ENABLED=true
MMR_LAMBDA=0.7
```

**功能说明**：
- **Reranker**：对初步检索结果进行深度语义重排序，提升精度 15-30%
- **MMR**：通过多样性算法减少冗余结果，提高信息覆盖率
- **可配置 RRF**：支持调整融合参数（k、向量权重、关键词权重）

未配置时自动使用离线启发式 Reranker（基于 jieba 分词），无需额外依赖。详见 [`docs/rerank-mmr-guide.md`](docs/rerank-mmr-guide.md)。

## 文档解析

`POST /api/v1/parser/preview` 可以在不入库的情况下查看完整解析结果。

可选能力：

```powershell
python -m pip install -e ".[ocr]"       # Python OCR 依赖；系统还需安装 Tesseract
```

启用 OCR：

```env
OCR_ENGINE=tesseract
```

扫描 PDF 只对文本不足的页面执行 OCR；未配置 OCR 时会产生 `scanned_pages_without_ocr` 警告。

## 测试

```powershell
python -X pycache_prefix=data/cache/python -m pytest -q
```

测试默认使用离线 Provider，不访问网络、不消耗 Token。

## Demo 边界

向量存储采用 SQLite JSON 加 Python 线性计算，适合教学和小数据集。生产环境可以保持 `RetrievalService` 上层不变，将 Dense 检索替换为 pgvector、Qdrant 或 Milvus。当前不包含 Agent、MCP、权限、多租户和知识图谱。
