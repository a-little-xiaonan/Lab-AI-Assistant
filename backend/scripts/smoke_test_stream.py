"""实测 DashScope 流式返回形态（手动运行，不进测试套件）。

用法：
    ../.venv/bin/python scripts/smoke_test_stream.py

验证目标（Step 01 SSE 的前置实测）：
1. Generation.call(stream=True) 的返回类型（同步生成器？）
2. incremental_output=True 与不传的差异（原生增量 vs merge 全量）
3. 首块 output.text 是否为空 / 块内 text 是增量还是累积
4. 中途块 status_code 的表现
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from dashscope import Generation  # noqa: E402

from app.config import settings  # noqa: E402


def _show(label: str, gen) -> None:
    print(f"\n===== {label} =====")
    print(f"类型: {type(gen)}")
    n = 0
    prev = ""
    for chunk in gen:
        n += 1
        status = chunk.status_code
        code = getattr(chunk, "code", None)
        msg = getattr(chunk, "message", None)
        text = getattr(getattr(chunk, "output", None), "text", None)
        is_inc = text is not None and text.startswith(prev) and text != prev
        print(
            f"#{n} status={status} code={code} msg={msg!r} "
            f"text={text!r} [{'增量' if is_inc else '全量/空'}]"
        )
        if text:
            prev = text
        if n >= 12:
            print("（截断展示）")
            break
    print(f"共 {n} 块")


def main() -> int:
    if not settings.dashscope_api_key:
        print("未配置 DASHSCOPE_API_KEY，请在 .env 填写后运行")
        return 1
    messages = [
        {"role": "system", "content": "你是一个测试助手，请用简短的句子回答。"},
        {"role": "user", "content": "请从 1 数到 10，每行一个数字。"},
    ]
    # 1. 默认流式（qwen 系文档说明默认走 merge，即每块全量累积文本）
    gen1 = Generation.call(
        model=settings.llm_model, messages=messages, api_key=settings.dashscope_api_key,
        stream=True, timeout=settings.llm_timeout,
    )
    _show("默认流式（不传 incremental_output）", gen1)

    # 2. 显式 incremental_output=True
    gen2 = Generation.call(
        model=settings.llm_model, messages=messages, api_key=settings.dashscope_api_key,
        stream=True, incremental_output=True, timeout=settings.llm_timeout,
    )
    _show("incremental_output=True", gen2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
