"""按链路、会话或消息 ID 查看本机诊断文件，不提供网络接口。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def records(files):
    for path in files:
        try:
            with path.open(encoding="utf-8") as source:
                for line in source:
                    try:
                        item = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(item, dict) and isinstance(item.get("trace_id"), str):
                        yield item
        except (OSError, UnicodeError):
            continue


def main() -> None:
    parser = argparse.ArgumentParser(description="只读查看服务端聊天追踪，缺失记录不会被补造为成功")
    parser.add_argument("--directory", type=Path, default=ROOT / "data/logs/traces")
    parser.add_argument("--trace-id")
    parser.add_argument("--request-id")
    parser.add_argument("--session-id")
    parser.add_argument("--message-id", type=int)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if not (args.trace_id or args.request_id or args.session_id or args.message_id is not None):
        parser.error("请至少指定 trace-id、request-id、session-id 或 message-id")
    if not 1 <= args.limit <= 100:
        parser.error("limit 必须在 1 到 100 之间")
    files = sorted(args.directory.glob("chat-trace-*.jsonl*"))
    matched = {}
    for item in records(files):
        if ((not args.trace_id or item["trace_id"] == args.trace_id)
                and (not args.request_id or item.get("request_id") == args.request_id)
                and (not args.session_id or item.get("session_id") == args.session_id)
                and (args.message_id is None or item.get("message_id") == args.message_id)):
            matched[item["trace_id"]] = str(item.get("timestamp", ""))
    selected = sorted(matched, key=matched.get, reverse=True)[:args.limit]
    grouped = {key: [] for key in selected}
    for item in records(files):
        if item["trace_id"] in grouped and len(grouped[item["trace_id"]]) < 10000:
            grouped[item["trace_id"]].append(item)
    if not grouped:
        print("未找到匹配记录；可能未启用观测、日志已轮转或记录丢失。")
    for trace_id, events in grouped.items():
        events.sort(key=lambda item: item.get("sequence", 0))
        root = [item for item in events if item.get("stage") == "chat.request" and item.get("event") == "end"]
        sequence = [item.get("sequence") for item in events]
        gaps = sequence != list(range(1, len(events) + 1))
        state = root[0].get("status") if len(root) == 1 else "incomplete"
        print(f"链路 {trace_id}：{state}" + ("（记录可能不完整）" if gaps or len(events) == 10000 else ""))
        parents = {item.get("span_id"): item.get("parent_span_id") for item in events}
        for item in events:
            if item.get("event") == "start":
                continue
            depth, current, seen = 0, item.get("parent_span_id"), set()
            while current and current not in seen and depth < 12:
                seen.add(current)
                depth += 1
                current = parents.get(current)
            attrs = json.dumps(item.get("attributes", {}), ensure_ascii=False)
            print(f"{'  ' * depth}{item.get('stage')} {item.get('status') or ''} {item.get('duration_ms')}ms {attrs}")
    print("说明：当前为第一阶段观测，不包含完整检索候选、Prompt 正文或持久化回答依据。")


if __name__ == "__main__":
    main()
