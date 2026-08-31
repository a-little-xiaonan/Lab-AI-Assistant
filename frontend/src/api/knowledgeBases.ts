// 知识库列表（下拉选择器用）
import type { KnowledgeBase } from "../types";

export async function listKnowledgeBases(): Promise<KnowledgeBase[]> {
  const resp = await fetch("/api/knowledge-bases");
  if (!resp.ok) throw new Error(`获取知识库列表失败（${resp.status}）`);
  return resp.json();
}
