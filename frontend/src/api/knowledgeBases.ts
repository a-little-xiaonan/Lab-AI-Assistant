// 知识库管理 API（Phase 3-04：建/删/详情/上传/删除文档/重索引/统计）
import type {
  ChunkList,
  DocumentItem,
  KnowledgeBase,
  KnowledgeBaseDetail,
  ReindexStatus,
  Stats,
} from "../types";

async function parseError(resp: Response): Promise<Error> {
  try {
    const body = await resp.json();
    const detail = body?.detail;
    if (typeof detail === "object" && detail !== null) {
      return new Error(detail.message || detail.code || `请求失败（${resp.status}）`);
    }
  } catch {
    /* 非 JSON 错误体 */
  }
  return new Error(`请求失败（${resp.status}）`);
}

export async function listKnowledgeBases(): Promise<KnowledgeBase[]> {
  const resp = await fetch("/api/knowledge-bases");
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function createKnowledgeBase(name: string, description?: string): Promise<KnowledgeBase> {
  const resp = await fetch("/api/knowledge-bases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description: description || null }),
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function deleteKnowledgeBase(kbId: string): Promise<void> {
  const resp = await fetch(`/api/knowledge-bases/${kbId}`, { method: "DELETE" });
  if (!resp.ok) throw await parseError(resp);
}

export async function getKnowledgeBaseDetail(kbId: string): Promise<KnowledgeBaseDetail> {
  const resp = await fetch(`/api/knowledge-bases/${kbId}`);
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export interface UploadResult {
  doc_id: string;
  filename: string;
  status: string;
  file_size: number;
  kb_id: string;
}

export async function uploadDocument(kbId: string, file: File): Promise<UploadResult> {
  const form = new FormData();
  form.append("file", file);
  const resp = await fetch(`/api/knowledge-bases/${kbId}/documents`, {
    method: "POST",
    body: form,
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function deleteDocument(kbId: string, docId: string): Promise<void> {
  const resp = await fetch(`/api/knowledge-bases/${kbId}/documents/${docId}`, {
    method: "DELETE",
  });
  if (!resp.ok) throw await parseError(resp);
}

export async function reindex(kbId: string, docId?: string): Promise<ReindexStatus> {
  const resp = await fetch(`/api/knowledge-bases/${kbId}/reindex`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(docId ? { doc_id: docId } : {}),
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function reindexStatus(kbId: string): Promise<ReindexStatus> {
  const resp = await fetch(`/api/knowledge-bases/${kbId}/reindex/status`);
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function getStats(): Promise<Stats> {
  const resp = await fetch("/api/stats");
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

/** 文档 chunk 明细（内容/大小/位置元数据，复用后端 Phase 1 接口） */
export async function getDocChunks(docId: string, limit = 200): Promise<ChunkList> {
  const resp = await fetch(`/api/documents/${docId}/chunks?limit=${limit}`);
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}
