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
