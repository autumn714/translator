export const MODEL_DOWN_MESSAGE = "모델 서버에 연결할 수 없습니다";

export class ApiError extends Error {
  constructor(message, { status = 0, network = false } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.network = network;
  }

  get modelDown() {
    return this.network || this.status === 502 || this.status === 503 || this.status === 504;
  }
}

export function redirectToLogin() {
  if (window.location.pathname !== "/login") window.location.assign("/login");
}

function fallbackMessage(status) {
  if (status === 400 || status === 422) return "입력값을 확인하세요.";
  if (status === 404) return "대상을 찾을 수 없습니다.";
  if (status === 413) return "파일이 너무 큽니다.";
  if (status === 415) return "지원하지 않는 형식입니다.";
  if (status === 429) return "요청이 많습니다. 잠시 후 다시 시도하세요.";
  if (status === 502 || status === 503 || status === 504) return MODEL_DOWN_MESSAGE;
  if (status >= 500) return "서버 오류가 발생했습니다.";
  return `요청을 처리하지 못했습니다 (${status})`;
}

export async function errorFromResponse(response) {
  let detail = "";
  try {
    const data = await response.json();
    if (typeof data?.detail === "string") detail = data.detail;
    else if (Array.isArray(data?.detail) && data.detail[0]?.msg) detail = "입력값을 확인하세요.";
  } catch {
    /* body is not JSON */
  }
  return new ApiError(detail || fallbackMessage(response.status), { status: response.status });
}

async function send(path, { method = "GET", json, body, signal, headers } = {}) {
  const init = { method, signal, credentials: "same-origin", headers: { Accept: "application/json", ...(headers || {}) } };
  if (json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (body !== undefined) {
    init.body = body;
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError("서버에 연결할 수 없습니다.", { network: true });
  }
  if (response.status === 401 && path !== "/api/login") {
    redirectToLogin();
    throw new ApiError("로그인이 필요합니다.", { status: 401 });
  }
  if (!response.ok) throw await errorFromResponse(response);
  return response;
}

export async function api(path, options = {}) {
  const response = await send(path, options);
  if (response.status === 204) return null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) return response.json();
  return response.text();
}

// Reads an NDJSON response body and calls onEvent for every parsed line.
export async function streamNdjson(path, payload, { signal, onEvent }) {
  const response = await send(path, {
    method: "POST",
    json: payload,
    signal,
    headers: { Accept: "application/x-ndjson" },
  });
  if (!response.body) {
    const text = await response.text();
    text.split("\n").forEach((line) => emitLine(line, onEvent));
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        emitLine(buffer.slice(0, newline), onEvent);
        buffer = buffer.slice(newline + 1);
        newline = buffer.indexOf("\n");
      }
    }
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    if (signal?.aborted) {
      const abort = new Error("aborted");
      abort.name = "AbortError";
      throw abort;
    }
    throw new ApiError("연결이 끊어졌습니다.", { network: true });
  }
  buffer += decoder.decode();
  emitLine(buffer, onEvent);
}

function emitLine(line, onEvent) {
  const trimmed = line.trim();
  if (!trimmed) return;
  let event;
  try {
    event = JSON.parse(trimmed);
  } catch {
    return;
  }
  onEvent(event);
}
