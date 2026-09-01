import { apiFetch, parseApiError } from "./client";
import type { UserMemory } from "../types";

export async function listMyMemories(): Promise<UserMemory[]> {
  const resp = await apiFetch("/api/users/me/memories");
  if (!resp.ok) throw await parseApiError(resp);
  return resp.json();
}

export async function deleteMyMemory(id: string): Promise<void> {
  const resp = await apiFetch(`/api/users/me/memories/${id}`, { method: "DELETE" });
  if (!resp.ok) throw await parseApiError(resp);
}

export async function clearMyMemories(): Promise<void> {
  const resp = await apiFetch("/api/users/me/memories", { method: "DELETE" });
  if (!resp.ok) throw await parseApiError(resp);
}
