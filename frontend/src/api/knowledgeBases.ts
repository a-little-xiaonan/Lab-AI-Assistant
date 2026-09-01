// 知识库管理 API（Phase 3-04：建/删/详情/上传/删除文档/重索引/统计）
import type {
  ChunkList,
  DocumentItem,
  KnowledgeBase,
  KnowledgeBaseDetail,
  KnowledgeBasePermissions,
  ReindexStatus,
  RetrievalTopic,
  Stats,
} from "../types";
import { apiFetch } from "./client";

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
  const resp = await apiFetch("/api/knowledge-bases");
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function createKnowledgeBase(
  name: string,
  description?: string,
  visibility: "public" | "authenticated" | "restricted" = "public",
): Promise<KnowledgeBase> {
  const resp = await apiFetch("/api/knowledge-bases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description: description || null, visibility }),
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function getKnowledgeBasePermissions(kbId: string): Promise<KnowledgeBasePermissions> {
  const resp = await apiFetch(`/api/knowledge-bases/${kbId}/permissions`);
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function grantKnowledgeBaseRolePermission(
  kbId: string,
  roleCode: string,
  permission: "read" | "write" | "manage",
): Promise<void> {
  const resp = await apiFetch(`/api/knowledge-bases/${kbId}/permissions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role_code: roleCode, permission }),
  });
  if (!resp.ok) throw await parseError(resp);
}

export async function revokeKnowledgeBaseRolePermission(kbId: string, permissionId: number): Promise<void> {
  const resp = await apiFetch(`/api/knowledge-bases/${kbId}/role-permissions/${permissionId}`, { method: "DELETE" });
  if (!resp.ok) throw await parseError(resp);
}

export async function deleteKnowledgeBase(kbId: string): Promise<void> {
  const resp = await apiFetch(`/api/knowledge-bases/${kbId}`, { method: "DELETE" });
  if (!resp.ok) throw await parseError(resp);
}

export async function getKnowledgeBaseDetail(kbId: string): Promise<KnowledgeBaseDetail> {
  const resp = await apiFetch(`/api/knowledge-bases/${kbId}`);
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
  const resp = await apiFetch(`/api/knowledge-bases/${kbId}/documents`, {
    method: "POST",
    body: form,
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function deleteDocument(kbId: string, docId: string): Promise<void> {
  const resp = await apiFetch(`/api/knowledge-bases/${kbId}/documents/${docId}`, {
    method: "DELETE",
  });
  if (!resp.ok) throw await parseError(resp);
}

export async function reindex(kbId: string, docId?: string): Promise<ReindexStatus> {
  const resp = await apiFetch(`/api/knowledge-bases/${kbId}/reindex`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(docId ? { doc_id: docId } : {}),
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function reindexStatus(kbId: string): Promise<ReindexStatus> {
  const resp = await apiFetch(`/api/knowledge-bases/${kbId}/reindex/status`);
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function getStats(): Promise<Stats> {
  const resp = await apiFetch("/api/stats");
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function listRetrievalTopics(): Promise<RetrievalTopic[]> {
  const resp = await apiFetch("/api/retrieval-topics");
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function updateDocumentTopics(kbId: string, docId: string, topicCodes: string[]): Promise<string[]> {
  const resp = await apiFetch(`/api/knowledge-bases/${kbId}/documents/${docId}/topics`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ topic_codes: topicCodes }),
  });
  if (!resp.ok) throw await parseError(resp);
  const data = await resp.json();
  return data.topic_codes;
}

/** 文档 chunk 明细（内容/大小/位置元数据，复用后端 Phase 1 接口） */
export async function getDocChunks(docId: string, limit = 200): Promise<ChunkList> {
  const resp = await apiFetch(`/api/documents/${docId}/chunks?limit=${limit}`);
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}
