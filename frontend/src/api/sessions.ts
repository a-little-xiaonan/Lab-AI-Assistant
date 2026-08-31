// 会话 CRUD（对齐 backend /api/chat/sessions）
import type { SessionDetail, SessionItem } from "../types";

export async function listSessions(): Promise<SessionItem[]> {
  const resp = await fetch("/api/chat/sessions");
  if (!resp.ok) throw new Error(`获取会话列表失败（${resp.status}）`);
  return resp.json();
}

export async function createSession(knowledgeBaseId?: string): Promise<SessionItem> {
  const resp = await fetch("/api/chat/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(knowledgeBaseId ? { knowledge_base_id: knowledgeBaseId } : {}),
  });
  if (!resp.ok) throw new Error(`创建会话失败（${resp.status}）`);
  return resp.json();
}

export async function getSession(sessionId: string): Promise<SessionDetail> {
  const resp = await fetch(`/api/chat/sessions/${sessionId}`);
  if (!resp.ok) throw new Error(`获取会话失败（${resp.status}）`);
  return resp.json();
}

export async function deleteSession(sessionId: string): Promise<void> {
  const resp = await fetch(`/api/chat/sessions/${sessionId}`, { method: "DELETE" });
  if (!resp.ok) throw new Error(`删除会话失败（${resp.status}）`);
}

export async function renameSession(sessionId: string, name: string): Promise<SessionItem> {
  const resp = await fetch(`/api/chat/sessions/${sessionId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!resp.ok) throw new Error(`重命名失败（${resp.status}）`);
  return resp.json();
}

/** 批量删除会话：sessionIds 缺省或传空 → 全部删除（后端语义） */
export async function batchDeleteSessions(sessionIds?: string[]): Promise<number> {
  const resp = await fetch("/api/chat/sessions", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sessionIds?.length ? { session_ids: sessionIds } : { all: true }),
  });
  if (!resp.ok) throw new Error(`删除失败（${resp.status}）`);
  const data = await resp.json();
  return data.deleted as number;
}
