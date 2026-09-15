import { apiFetch, parseApiError } from "./client";
import type { RoleApplication } from "../types";

export async function applyForEditor(reason: string, evidenceText?: string): Promise<RoleApplication> {
  const resp = await apiFetch("/api/users/me/role-applications", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_role: "editor", reason, evidence_text: evidenceText || null }),
  });
  if (!resp.ok) throw await parseApiError(resp);
  return resp.json();
}
export async function listMyApplications(): Promise<RoleApplication[]> {
  const resp = await apiFetch("/api/users/me/role-applications");
  if (!resp.ok) throw await parseApiError(resp); return resp.json();
}
export async function cancelApplication(id: string): Promise<void> {
  const resp = await apiFetch(`/api/users/me/role-applications/${id}/cancel`, { method: "POST" });
  if (!resp.ok) throw await parseApiError(resp);
}
export async function listApplications(status = "pending"): Promise<{ total: number; items: RoleApplication[] }> {
  const resp = await apiFetch(`/api/admin/role-applications?status=${status}`);
  if (!resp.ok) throw await parseApiError(resp); return resp.json();
}
export async function reviewApplication(id: string, approve: boolean, comment?: string): Promise<void> {
  const resp = await apiFetch(`/api/admin/role-applications/${id}/${approve ? "approve" : "reject"}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ comment: comment || null }),
  });
  if (!resp.ok) throw await parseApiError(resp);
}
