"""Context Cache P0 冒烟验证：发送相同前缀、不同问题的两次显式缓存请求。

用法：
    cd backend
    CONTEXT_CACHE_POC_ENABLED=true \\
      ../.venv/bin/python scripts/smoke/smoke_context_cache.py --model <已开通模型>

脚本仅使用模拟公开资料，不读取知识库、会话或用户问题。第二次调用若返回
cached_tokens > 0，说明当前模型/协议可以观测到缓存命中；否则请结合返回
字段、模型能力和业务空间配置排查，不能据此修改聊天主链路。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.llm.errors import LLMError  # noqa: E402
from app.llm.qwen import probe_context_cache  # noqa: E402


def _stable_prefix(namespace: str) -> str:
    """构造超过最低缓存长度的模拟资料；不可替换为真实内部文本。"""
    rules = (
        "你是实验室公开招新资料助手。仅根据资料回答；资料不足时明确说明。"
        "不得编造报名截止日期、联系方式或成员信息。回答简洁，并列出资料编号。\n"
    )
    card = "[E1] 模拟公开招新资料：零基础可以报名，报名后参加培训与考核。\n"
    # 中文字符近似 Token，重复后的稳定前缀明显高于供应商 1024 Token 的最小条件。
    # namespace 必须位于第一个内容块，避免供应商复用其后的公共片段；同一轮的
    # 冷、热请求仍完全一致，从而构成独立的冷/热对照。
    return f"POC 批次隔离：{namespace}。\n" + rules + card * 180


def _summary(label: str, result) -> dict:
    data = asdict(result)
    data.pop("answer", None)  # 输出只保留脱敏指标，避免误把模型回答当评测结论。
    data["label"] = label
    data["cache_hit"] = result.usage.cache_hit
    data["answer_chars"] = len(result.answer)
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="DashScope Context Cache P0 冒烟验证")
    parser.add_argument("--model", default="", help="已在当前业务空间开通的支持模型；为空时读取配置")
    parser.add_argument("--rounds", type=int, default=5, help="独立冷/热对照轮数，默认 5")
    args = parser.parse_args()
    if not settings.context_cache_poc_enabled:
        parser.error("请显式设置 CONTEXT_CACHE_POC_ENABLED=true 后再运行")

    if args.rounds < 1 or args.rounds > 20:
        parser.error("--rounds 必须在 1 到 20 之间")
    selected_model = args.model or settings.context_cache_poc_model or settings.llm_model
    rounds = []
    for index in range(args.rounds):
        try:
            prefix = _stable_prefix(f"p0-{index + 1}")
            cold = probe_context_cache(prefix, "零基础学生能报名吗？", selected_model)
            warm = probe_context_cache(prefix, "非计算机专业的新生可以参加培训吗？", selected_model)
        except LLMError as exc:
            print(json.dumps({"status": "failed", "round": index + 1,
                              "code": exc.code, "message": exc.message}, ensure_ascii=False))
            return 1
        rounds.append({"round": index + 1, "cold": _summary("cold", cold),
                       "warm": _summary("warm", warm)})

    cold_ttft = [item["cold"]["ttft_seconds"] for item in rounds if item["cold"]["ttft_seconds"] is not None]
    warm_ttft = [item["warm"]["ttft_seconds"] for item in rounds if item["warm"]["ttft_seconds"] is not None]
    warm_hits = sum(item["warm"]["cache_hit"] is True for item in rounds)
    summary = {
        "warm_cache_hit_rate": warm_hits / len(rounds),
        "cold_ttft_p50_seconds": statistics.median(cold_ttft) if cold_ttft else None,
        "warm_ttft_p50_seconds": statistics.median(warm_ttft) if warm_ttft else None,
        "cold_ttft_p95_seconds": max(cold_ttft) if cold_ttft else None,
        "warm_ttft_p95_seconds": max(warm_ttft) if warm_ttft else None,
    }

    payload = {
        "status": "ok",
        "model": selected_model,
        "round_count": len(rounds),
        "prefix_chars": len(_stable_prefix("length-check")),
        "rounds": rounds,
        "summary": summary,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if any(item["warm"]["usage"]["cached_tokens"] is None for item in rounds):
        print("未观察到缓存用量字段；请确认模型、地域和业务空间是否支持该协议。")
        return 2
    if warm_hits != len(rounds):
        print("部分热请求未命中缓存；请检查前缀一致性、缓存有效期和模型支持情况。")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
