"""DashScope 冒烟测试：对话 + Embedding 链路验证（手动执行，不入测试套件）。

用法：
    uv run python scripts/smoke_test.py
    # 或
    .venv/bin/python scripts/smoke_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ 加入 import 路径

from app.config import settings  # noqa: E402
from app.llm.qwen import EMBEDDING_DIM, chat_completion, embed_texts  # noqa: E402


def main() -> int:
    if not settings.dashscope_api_key:
        print("[FAIL] 未配置 DASHSCOPE_API_KEY")
        print("  请复制 .env.example 为 .env，填入阿里云百炼创建的 API Key 后重试")
        return 1

    # 1. 对话
    print(f"[1/2] 对话测试（model={settings.llm_model}）...")
    reply = chat_completion([{"role": "user", "content": "用一句话介绍你自己"}])
    print(f"      回复：{reply[:80]}")

    # 2. Embedding
    print(f"[2/2] Embedding 测试（model={settings.embedding_model}）...")
    vecs = embed_texts(["hello world", "通义千问"])
    assert all(len(v) == EMBEDDING_DIM for v in vecs), f"维度应为 {EMBEDDING_DIM}"
    vecs2 = embed_texts(["hello world", "通义千问"])
    assert vecs == vecs2, "相同文本两次向量化结果应一致（确定性）"
    print(f"      返回 {len(vecs)} 条，维度 {len(vecs[0])}，确定性校验通过")

    print("[OK] 冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
