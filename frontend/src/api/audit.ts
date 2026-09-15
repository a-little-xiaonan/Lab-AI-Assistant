import { apiFetch, parseApiError } from "./client";
import type { AuditLogItem } from "../types";
export async function listAuditLogs(params: Record<string, string | number> = {}): Promise<{ total: number; items: AuditLogItem[] }> {
  const query = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]));
  const resp = await apiFetch(`/api/admin/audit-logs?${query}`);
  if (!resp.ok) throw await parseApiError(resp); return resp.json();
}
