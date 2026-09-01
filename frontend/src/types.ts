// 前后端共享的类型（对齐 backend/app/models/schemas.py）

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string | null;
  embedding_model: string;
  access_level: "guest" | "student" | "editor" | "admin";
  document_count: number;
  chunk_count: number;
  created_at: string;
}

export interface KnowledgeBaseRolePermission {
  id: number;
  role_code: string;
  role_name: string;
  permission: "read" | "write" | "manage";
}

export interface KnowledgeBaseUserPermission {
  id: number;
  user_id: string;
  username: string;
  nickname: string;
  permission: "read" | "write" | "manage";
}

export interface KnowledgeBasePermissions {
  role_permissions: KnowledgeBaseRolePermission[];
  user_permissions: KnowledgeBaseUserPermission[];
}

export interface Source {
  source_file: string;
  page: number | null;
  snippet: string;
}

export interface SessionItem {
  id: string;
  knowledge_base_id: string;
  name: string | null;
  created_at: string;
  updated_at: string;
}

export interface MessageItem {
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

export interface SessionDetail extends SessionItem {
  messages: MessageItem[];
}

// SSE 事件（Phase 2-01 协议）
export type SSEEvent =
  | { event: "meta"; data: { session_id: string } }
  | { event: "delta"; data: { text: string } }
  | { event: "done"; data: { full_text: string; sources: Source[] } }
  | { event: "error"; data: { code: string; message: string } };

export interface ChatPayload {
  session_id?: string | null;
  // 聊天范围由后端按当前身份计算，客户端不再传知识库 ID。
  message: string;
  stream: boolean;
}

export interface AuthUser {
  id: string;
  username: string;
  nickname: string;
  email: string | null;
  roles: string[];
  status: string;
  created_at: string;
}

export interface UserMemory {
  id: string;
  memory_type: string;
  content: string;
  confidence: number;
  source_session_id: string | null;
  scope_kb_id: string | null;
  created_at: string;
  updated_at: string;
}

// ---- Phase 3-04 知识库管理 ----

export interface DocumentItem {
  doc_id: string;
  filename: string;
  file_size: number;
  status: string; // processing / ready / failed / reindexing
  error_message: string | null;
  chunk_count: number;
  topics: string[];
  topic_suggestions: TopicSuggestion[];
  created_at: string;
}

export interface TopicSuggestion {
  topic_code: string;
  source: string;
  confidence: number | null;
  review_status: "pending" | "rejected";
}

export interface RetrievalTopic {
  code: string;
  name: string;
  aliases: string[];
}

export interface KnowledgeBaseDetail extends KnowledgeBase {
  documents: DocumentItem[];
}

export interface Stats {
  document_count: number;
  chunk_count: number;
  storage_size: number;
  knowledge_base_count: number;
  vector_dim: number;
  knowledge_bases: { id: string; name: string; document_count: number; chunk_count: number }[];
}

export interface ReindexStatus {
  kb_id: string;
  doc_id: string | null;
  status: string; // idle / running / done / failed
  total: number;
  done: number;
  current_doc: string | null;
  docs_before: number | null;
  docs_after: number | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface ChunkItem {
  chunk_index: number;
  text: string;
  char_length: number;
  token_estimate: number;
  page: number | null;
  slide_number: number | null;
  sheet_name: string | null;
  row_range: string | null;
  created_at: string;
  updated_at: string;
}

export interface ChunkList {
  doc_id: string;
  filename: string;
  total: number;
  offset: number;
  limit: number;
  chunks: ChunkItem[];
}
