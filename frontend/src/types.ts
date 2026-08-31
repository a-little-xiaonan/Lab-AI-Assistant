// 前后端共享的类型（对齐 backend/app/models/schemas.py）

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string | null;
  embedding_model: string;
  document_count: number;
  chunk_count: number;
  created_at: string;
}

export interface Source {
  source_file: string;
  page: number | null;
  snippet: string;
}

export interface SessionItem {
  id: string;
  knowledge_base_id: string;
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
  knowledge_base_id: string;
  message: string;
  stream: boolean;
}

// ---- Phase 3-04 知识库管理 ----

export interface DocumentItem {
  doc_id: string;
  filename: string;
  file_size: number;
  status: string; // processing / ready / failed / reindexing
  error_message: string | null;
  chunk_count: number;
  created_at: string;
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
}

export interface ChunkList {
  doc_id: string;
  filename: string;
  total: number;
  offset: number;
  limit: number;
  chunks: ChunkItem[];
}
