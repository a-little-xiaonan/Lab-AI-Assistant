// 聊天 API：流式用 fetch + ReadableStream 解析 SSE（不用 EventSource——需要自定义 header 与中止）
import type { ChatPayload, SSEEvent } from "../types";
import { apiFetch } from "./client";

async function parseError(resp: Response): Promise<Error> {
  try {
    const body = await resp.json();
    const detail = body?.detail;
    if (typeof detail === "object" && detail !== null) {
      return new Error(detail.message || detail.code || `请求失败（${resp.status}）`);
    }
  } catch {
    /* 非 JSON 错误体，走兜底文案 */
  }
  return new Error(`请求失败（${resp.status}）`);
}

/** 非流式问答 */
export async function chatOnce(payload: ChatPayload) {
  const resp = await apiFetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, stream: false }),
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

/** 解析一段 SSE 帧文本（event:/data: 行，data 多行用 \n 拼接） */
function parseFrame(frame: string): SSEEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length) return null;
  const data = JSON.parse(dataLines.join("\n"));
  return { event, data } as SSEEvent;
}

/** 流式问答：消费 SSE 事件序列（meta → delta* → done | error），支持 AbortController 中止 */
export async function* chatStream(
  payload: ChatPayload,
  signal?: AbortSignal,
): AsyncGenerator<SSEEvent> {
  const resp = await apiFetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, stream: true }),
    signal,
  });
  if (!resp.ok || !resp.body) throw await parseError(resp);

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const evt = parseFrame(frame);
        if (evt) yield evt;
      }
    }
    // 收尾：残留帧防御性解析
    if (buffer.trim()) {
      const evt = parseFrame(buffer);
      if (evt) yield evt;
    }
  } finally {
    reader.releaseLock();
  }
}
