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
