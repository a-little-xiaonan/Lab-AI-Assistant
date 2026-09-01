import { apiFetch, clearAccessToken, parseApiError, setAccessToken } from "./client";
import type { AuthUser } from "../types";

export async function register(payload: { username: string; password: string; nickname: string; email?: string }) {
  const resp = await apiFetch("/api/auth/register", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  if (!resp.ok) throw await parseApiError(resp);
  return resp.json() as Promise<AuthUser>;
}

export async function login(username: string, password: string) {
  const resp = await apiFetch("/api/auth/login", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }),
  });
  if (!resp.ok) throw await parseApiError(resp);
  const data = await resp.json();
  setAccessToken(data.access_token);
  return data as { access_token: string; user: AuthUser };
}

export async function refresh() {
  const resp = await apiFetch("/api/auth/refresh", { method: "POST" });
  if (!resp.ok) throw await parseApiError(resp);
  const data = await resp.json();
  setAccessToken(data.access_token);
  return data as { access_token: string; user: AuthUser };
}

export async function logout() {
  const resp = await apiFetch("/api/auth/logout", { method: "POST" });
  clearAccessToken();
  if (!resp.ok && resp.status !== 204) throw await parseApiError(resp);
}
