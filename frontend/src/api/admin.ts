import { apiFetch, parseApiError } from "./client";
import type { AuthUser } from "../types";

export async function listUsers(): Promise<AuthUser[]> {
  const resp = await apiFetch("/api/admin/users");
  if (!resp.ok) throw await parseApiError(resp);
  return resp.json();
}

export async function updateUserRoles(userId: string, roles: string[]): Promise<AuthUser> {
  const resp = await apiFetch(`/api/admin/users/${userId}/roles`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ roles }),
  });
  if (!resp.ok) throw await parseApiError(resp);
  return resp.json();
}

export async function updateUserStatus(userId: string, status: "active" | "disabled"): Promise<AuthUser> {
  const resp = await apiFetch(`/api/admin/users/${userId}/status`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!resp.ok) throw await parseApiError(resp);
  return resp.json();
}
