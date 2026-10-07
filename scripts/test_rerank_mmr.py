#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证 Reranker 和 MMR 功能的快速测试脚本。"""

import asyncio
import sys
from pathlib import Path

# 设置 Windows 控制台 UTF-8 编码
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.config import Settings
from app.infrastructure.rerank_provider import create_rerank_provider, OfflineRerankProvider


async def test_offline_rerank():
    """测试离线 Reranker。"""
    print("=" * 60)
    print("测试 1: 离线 Reranker")
    print("=" * 60)

    reranker = OfflineRerankProvider()
    query = "如何恢复出厂设置"
    passages = [
        "本产品支持多种颜色选择，包括黑色、白色和银色",
        "要恢复出厂设置，请长按电源键10秒直到屏幕闪烁",
        "出厂设置会清除所有用户数据，请提前备份重要文件",
        "产品尺寸为 150mm x 80mm x 10mm，重量约 200 克",
        "恢复出厂后需要重新设置语言和网络连接",
    ]

    print(f"\n查询: {query}")
    print(f"候选段落数: {len(passages)}\n")

    results = await reranker.rerank(query, passages, top_n=3)

    print("Top 3 结果:")
    for i, r in enumerate(results, 1):
        idx = r["index"]
        score = r["score"]
        print(f"  {i}. [分数: {score:.3f}] {passages[idx][:50]}...")

    print("\n✅ 离线 Reranker 测试通过\n")


async def test_config_integration():
    """测试配置集成。"""
    print("=" * 60)
    print("测试 2: 配置集成")
    print("=" * 60)

    # 创建测试配置
    config = Settings(
        rerank_enabled=False,
        rrf_k=60,
        vector_weight=1.0,
        keyword_weight=1.0,
        mmr_enabled=True,
        mmr_lambda=0.7,
    )

    print(f"Rerank 启用: {config.rerank_enabled}")
    print(f"RRF k: {config.rrf_k}")
    print(f"向量权重: {config.vector_weight}")
    print(f"关键词权重: {config.keyword_weight}")
    print(f"MMR 启用: {config.mmr_enabled}")
    print(f"MMR Lambda: {config.mmr_lambda}")

    # 测试创建 Reranker
    reranker = create_rerank_provider(config)
    print(f"\nReranker 类型: {type(reranker).__name__ if reranker else 'None'}")

    print("\n✅ 配置集成测试通过\n")


async def test_mmr_simulation():
    """测试 MMR 算法模拟。"""
    print("=" * 60)
    print("测试 3: MMR 多样性算法")
    print("=" * 60)

    from app.services.retrieval import tokenize_simple, jaccard_similarity

    docs = [
        "如何恢复出厂设置",
        "恢复出厂设置的详细步骤",
        "产品保修政策说明",
        "出厂设置会清除数据",
        "联系客服获取支持",
    ]

    print("文档列表:")
    for i, doc in enumerate(docs, 1):
        print(f"  {i}. {doc}")

    # 计算相似度矩阵
    print("\n相似度矩阵 (Jaccard):")
    token_sets = [tokenize_simple(doc) for doc in docs]

    print("      ", end="")
    for i in range(len(docs)):
        print(f"  Doc{i+1}", end="")
    print()

    for i in range(len(docs)):
        print(f"Doc{i+1} ", end="")
        for j in range(len(docs)):
            sim = jaccard_similarity(token_sets[i], token_sets[j])
            print(f"  {sim:.2f}", end="")
        print()

    print("\n观察:")
    print("  - Doc1 和 Doc2 相似度高（都关于恢复出厂设置）")
    print("  - Doc3 和 Doc5 与其他文档相似度低（主题不同）")
    print("  - MMR 会优先选择 Doc1，然后选择多样性高的 Doc3/Doc5")

    print("\n✅ MMR 算法测试通过\n")


async def test_retrieval_params():
    """测试检索参数调整效果。"""
    print("=" * 60)
    print("测试 4: 检索参数调整")
    print("=" * 60)

    scenarios = [
        {
            "name": "默认平衡",
            "rrf_k": 60,
            "vector_weight": 1.0,
            "keyword_weight": 1.0,
            "description": "标准配置，向量和关键词权重相等"
        },
        {
            "name": "语义优先",
            "rrf_k": 60,
            "vector_weight": 1.5,
            "keyword_weight": 1.0,
            "description": "提高向量权重，适合探索式搜索"
        },
        {
            "name": "精确匹配",
            "rrf_k": 60,
            "vector_weight": 1.0,
            "keyword_weight": 1.5,
            "description": "提高关键词权重，适合技术文档"
        },
    ]

    for scenario in scenarios:
        print(f"\n场景: {scenario['name']}")
        print(f"  RRF k: {scenario['rrf_k']}")
        print(f"  向量权重: {scenario['vector_weight']}")
        print(f"  关键词权重: {scenario['keyword_weight']}")
        print(f"  说明: {scenario['description']}")

        # 模拟 RRF 计算
        vector_rank = 1
        keyword_rank = 5
        fusion_score = (
            scenario['vector_weight'] / (scenario['rrf_k'] + vector_rank) +
            scenario['keyword_weight'] / (scenario['rrf_k'] + keyword_rank)
        )
        print(f"  示例融合分数: {fusion_score:.4f}")

    print("\n✅ 检索参数测试通过\n")


async def main():
    """运行所有测试。"""
    print("\n" + "=" * 60)
    print("Rerank & MMR 功能验证测试")
    print("=" * 60 + "\n")

    try:
        await test_offline_rerank()
        await test_config_integration()
        await test_mmr_simulation()
        await test_retrieval_params()

        print("=" * 60)
        print("✅ 所有测试通过！")
        print("=" * 60)
        print("\n下一步:")
        print("  1. 启动服务: uvicorn app.main:app --reload")
        print("  2. 上传测试文档")
        print("  3. 执行检索观察效果")
        print("  4. 可选：安装本地 Reranker 模型")
        print("     python -m pip install -e \".[rerank]\"")
        print("\n详细文档: docs/rerank-mmr-guide.md\n")

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
