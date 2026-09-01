/** 统一 API 客户端：自动携带短期 Access Token 与 Refresh Cookie。 */
let accessToken = "";

export function setAccessToken(token: string) {
  accessToken = token;
}

export function clearAccessToken() {
  accessToken = "";
}

export async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  return fetch(input, { ...init, headers, credentials: "include" });
}

export async function parseApiError(resp: Response): Promise<Error> {
  try {
    const body = await resp.json();
    const detail = body?.detail;
    if (detail && typeof detail === "object") return new Error(detail.message || detail.code);
  } catch {
    // 非 JSON 响应走状态码兜底。
  }
  return new Error(`请求失败（${resp.status}）`);
}
