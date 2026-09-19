import { apiFetch, parseApiError } from "../client";
import type { EvaluationRunItem } from "../../types";

export async function listEvaluationRuns(): Promise<{ total: number; items: EvaluationRunItem[] }> {
  const resp = await apiFetch("/api/admin/evaluation-runs?limit=50");
  if (!resp.ok) throw await parseApiError(resp);
  return resp.json();
}

export async function startEvaluation(
  mode: "retrieval" | "full",
  split: "dev" | "holdout" | "all",
): Promise<{ run_id: string; status: string }> {
  const resp = await apiFetch("/api/admin/evaluation-runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode, split }),
  });
  if (!resp.ok) throw await parseApiError(resp);
  return resp.json();
}
