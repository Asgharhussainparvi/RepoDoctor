import { useCallback, useEffect, useRef, useState } from "react";
import { type Api, ApiError } from "./api";
import { TERMINAL } from "./types";
import type { AgentEvent, Health, Repository, Session, UiState } from "./types";

export type Pending = null | "sample" | "upload" | "start" | "approve" | "reject" | "cancel";
export interface WorkflowOptions { pollMs: number; healthMs: number }

export const describe = (e: unknown): string =>
  e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Something unexpected went wrong.";

export function computeUiState(pending: Pending, session: Session | null): UiState {
  if (pending === "upload" || pending === "sample") return "uploading";
  if (!session) return "idle";
  if (pending === "start" && session.state === "INITIALIZE") return "analyzing";
  return session.ui_state;
}

function sessionIdFromHash(): string | null {
  const m = /^#s=([0-9a-f]{12})$/.exec(window.location.hash);
  return m ? m[1] : null;
}

export function useWorkflow(api: Api, { pollMs, healthMs }: WorkflowOptions) {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [repo, setRepo] = useState<Repository | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [pending, setPending] = useState<Pending>(null);
  const [error, setError] = useState<string | null>(null);
  const sessionId = session?.session_id ?? null;
  const active = !!session && !TERMINAL.has(session.state);
  const idRef = useRef<string | null>(null);
  idRef.current = sessionId;

  // ---- health ----
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api.getHealth().then(
        (h) => { if (!cancelled) { setHealth(h); setHealthError(null); } },
        (e) => { if (!cancelled) { setHealth(null); setHealthError(describe(e)); } },
      );
    void load();
    if (healthMs <= 0) return () => { cancelled = true; };
    const timer = setInterval(load, healthMs);
    return () => { cancelled = true; clearInterval(timer); };
  }, [api, healthMs]);

  // ---- reattach to a session after a page refresh (#s=<id>) ----
  useEffect(() => {
    const id = sessionIdFromHash();
    if (!id) return;
    let cancelled = false;
    (async () => {
      try {
        const s = await api.getSession(id);
        if (cancelled) return;
        setSession(s);
        if (s.repository_id) setRepo(await api.getRepository(s.repository_id));
      } catch { if (!cancelled) window.history.replaceState(null, "", window.location.pathname); }
    })();
    return () => { cancelled = true; };
  }, [api]);

  const refresh = useCallback(async () => {
    const id = idRef.current;
    if (!id) return;
    try {
      const s = await api.getSession(id);
      if (idRef.current === id) setSession(s);
    } catch (e) { setError(describe(e)); }
  }, [api]);

  // ---- live updates: SSE for the timeline, plus a slow poll as a safety net ----
  useEffect(() => {
    if (!sessionId) return;
    const close = api.openEvents(sessionId, {
      onStep: (event) => {
        setEvents((prev) => (prev.some((p) => p.seq === event.seq) ? prev : [...prev, event]));
        void refresh();
      },
      onSnapshot: (s) => { if (idRef.current === s.session_id) setSession(s); },
      onError: () => { void refresh(); },
    });
    return close;
  }, [api, sessionId, refresh]);

  useEffect(() => {
    if (!active || pollMs <= 0) return;
    const timer = setInterval(() => void refresh(), pollMs);
    return () => clearInterval(timer);
  }, [active, pollMs, refresh]);

  // ---- actions ----
  const run = useCallback(async (kind: Exclude<Pending, null>, work: () => Promise<void>) => {
    setPending(kind);
    setError(null);
    try { await work(); } catch (e) { setError(describe(e)); } finally { setPending(null); }
  }, []);

  const loadRepository = (r: Repository) => api.getRepository(r.id).then(setRepo, () => setRepo(r));

  const useSample = () => run("sample", async () => {
    resetSession();
    await loadRepository(await api.registerSample());
  });

  const upload = (file: File) => run("upload", async () => {
    if (!file.name.toLowerCase().endsWith(".zip")) throw new Error("Please choose a .zip file of your repository.");
    resetSession();
    await loadRepository(await api.uploadRepository(file));
  });

  const start = () => run("start", async () => {
    if (!repo) return;
    let current = session;
    if (!current || current.state !== "INITIALIZE") {
      current = await api.createSession(repo.id);
      setEvents([]);
      setSession(current);
      window.history.replaceState(null, "", `#s=${current.session_id}`);
    }
    setSession(await api.startSession(current.session_id));
  });

  const decide = (kind: "approve" | "reject") => run(kind, async () => {
    const patch = session?.pending_patch;
    if (!session || !patch) return;
    setSession(await (kind === "approve" ? api.approve : api.reject)(session.session_id, patch.sha256));
  });

  const cancel = () => run("cancel", async () => {
    if (session) setSession(await api.cancel(session.session_id));
  });

  function resetSession() {
    setSession(null);
    setEvents([]);
    window.history.replaceState(null, "", window.location.pathname);
  }

  return {
    health, healthError, repo, session, events, pending, error, uiState: computeUiState(pending, session),
    actions: {
      useSample, upload, start, cancel, reset: resetSession,
      approve: () => decide("approve"), reject: () => decide("reject"), dismissError: () => setError(null),
    },
  };
}
