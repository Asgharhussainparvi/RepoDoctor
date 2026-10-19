import type { AgentEvent, Health, Repository, Session } from "./types";

export class ApiError extends Error {
  constructor(public code: string, message: string, public status: number) {
    super(message);
  }
}

export interface EventHandlers {
  onStep: (event: AgentEvent) => void;
  onSnapshot: (session: Session) => void;
  onError: () => void;
}

/** Everything the UI needs from the server. Tests replace it with a fake. */
export interface Api {
  getHealth(): Promise<Health>;
  registerSample(): Promise<Repository>;
  uploadRepository(file: File): Promise<Repository>;
  getRepository(id: string): Promise<Repository>;
  createSession(repositoryId: string): Promise<Session>;
  startSession(id: string): Promise<Session>;
  getSession(id: string): Promise<Session>;
  approve(id: string, sha256: string): Promise<Session>;
  reject(id: string, sha256: string): Promise<Session>;
  cancel(id: string): Promise<Session>;
  openEvents(id: string, handlers: EventHandlers): () => void;
}

export const NETWORK_MESSAGE = "Cannot reach the RepoDoctor server. Is it running? (python -m repodoctor.api)";

export function createApi(
  fetchImpl: typeof fetch = (...args) => fetch(...args),
  EventSourceImpl: typeof EventSource | undefined = typeof EventSource === "undefined" ? undefined : EventSource,
): Api {
  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    let response: Response;
    try {
      response = await fetchImpl(path, init);
    } catch {
      throw new ApiError("network", NETWORK_MESSAGE, 0);
    }
    if (!response.ok) {
      let code = "http_error";
      let message = `The server answered with an error (${response.status}).`;
      try {
        const body = await response.json();
        if (body?.error?.message) { code = body.error.code ?? code; message = body.error.message; }
      } catch { /* not JSON: keep the generic message */ }
      throw new ApiError(code, message, response.status);
    }
    return (await response.json()) as T;
  }

  const post = <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "POST",
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });

  return {
    getHealth: () => request<Health>("/api/health"),
    registerSample: () => post<Repository>("/api/repositories/sample"),
    uploadRepository: (file) => {
      const form = new FormData();
      form.append("file", file);
      return request<Repository>("/api/repositories", { method: "POST", body: form });
    },
    getRepository: (id) => request<Repository>(`/api/repositories/${id}`),
    createSession: (repositoryId) => post<Session>("/api/debug-sessions", { repository_id: repositoryId }),
    startSession: (id) => post<Session>(`/api/debug-sessions/${id}/start`),
    getSession: (id) => request<Session>(`/api/debug-sessions/${id}`),
    approve: (id, sha) => post<Session>(`/api/debug-sessions/${id}/approve`, { patch_sha256: sha }),
    reject: (id, sha) => post<Session>(`/api/debug-sessions/${id}/reject`, { patch_sha256: sha }),
    cancel: (id) => post<Session>(`/api/debug-sessions/${id}/cancel`),
    openEvents(id, handlers) {
      if (!EventSourceImpl) { handlers.onError(); return () => {}; }
      const source = new EventSourceImpl(`/api/debug-sessions/${id}/events`);
      source.addEventListener("step", (e) => handlers.onStep(JSON.parse((e as MessageEvent).data)));
      source.addEventListener("snapshot", (e) => {
        handlers.onSnapshot(JSON.parse((e as MessageEvent).data));
        source.close();
      });
      source.onerror = () => handlers.onError();
      return () => source.close();
    },
  };
}
