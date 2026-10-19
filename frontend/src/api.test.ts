import { ApiError, NETWORK_MESSAGE, createApi } from "./api";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("api client", () => {
  it("turns the server's error envelope into an ApiError", async () => {
    const api = createApi(async () => json({ error: { code: "patch_mismatch", message: "No pending patch matches this hash." } }, 409));
    await expect(api.approve("id", "a".repeat(64))).rejects.toMatchObject({ code: "patch_mismatch", status: 409, message: "No pending patch matches this hash." });
  });

  it("reports an unreachable server in plain words", async () => {
    const api = createApi(async () => { throw new TypeError("Failed to fetch"); });
    await expect(api.getHealth()).rejects.toMatchObject({ code: "network", message: NETWORK_MESSAGE });
  });

  it("copes with error bodies that are not JSON", async () => {
    const api = createApi(async () => new Response("<html>oops</html>", { status: 502 }));
    const err = await api.getHealth().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 502, code: "http_error" });
    expect(err.message).toContain("502");
  });

  it("posts approval as JSON and uploads as multipart without a manual content type", async () => {
    const seen: { url: string; init?: RequestInit }[] = [];
    const api = createApi(async (url, init) => { seen.push({ url: String(url), init }); return json({}); });
    await api.approve("abc", "d".repeat(64));
    await api.uploadRepository(new File(["x"], "r.zip"));
    await api.createSession("rid");
    expect(seen[0]).toMatchObject({ url: "/api/debug-sessions/abc/approve", init: { method: "POST" } });
    expect(JSON.parse(seen[0].init!.body as string)).toEqual({ patch_sha256: "d".repeat(64) });
    expect(seen[1].url).toBe("/api/repositories");
    expect(seen[1].init!.body).toBeInstanceOf(FormData);
    expect((seen[1].init!.body as FormData).get("file")).toBeInstanceOf(File);
    expect(seen[1].init!.headers).toBeUndefined();
    expect(JSON.parse(seen[2].init!.body as string)).toEqual({ repository_id: "rid" });
  });
});

class FakeEventSource {
  static last: FakeEventSource;
  listeners: Record<string, (e: MessageEvent) => void> = {};
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) { FakeEventSource.last = this; }
  addEventListener(name: string, fn: (e: MessageEvent) => void) { this.listeners[name] = fn; }
  close() { this.closed = true; }
}

describe("event stream", () => {
  const make = () => createApi(async () => json({}), FakeEventSource as unknown as typeof EventSource);

  it("delivers steps and the final snapshot, then closes", () => {
    const onStep = vi.fn(); const onSnapshot = vi.fn();
    make().openEvents("abc", { onStep, onSnapshot, onError: vi.fn() });
    const es = FakeEventSource.last;
    expect(es.url).toBe("/api/debug-sessions/abc/events");
    es.listeners.step({ data: JSON.stringify({ seq: 1, message: "hi" }) } as MessageEvent);
    es.listeners.snapshot({ data: JSON.stringify({ state: "SUCCESS" }) } as MessageEvent);
    expect(onStep).toHaveBeenCalledWith({ seq: 1, message: "hi" });
    expect(onSnapshot).toHaveBeenCalledWith({ state: "SUCCESS" });
    expect(es.closed).toBe(true);
  });

  it("reports errors and can be closed by the caller", () => {
    const onError = vi.fn();
    const close = make().openEvents("abc", { onStep: vi.fn(), onSnapshot: vi.fn(), onError });
    FakeEventSource.last.onerror?.();
    expect(onError).toHaveBeenCalled();
    close();
    expect(FakeEventSource.last.closed).toBe(true);
  });

  it("falls back gracefully when the browser has no EventSource", () => {
    const onError = vi.fn();
    createApi(async () => json({}), undefined).openEvents("abc", { onStep: vi.fn(), onSnapshot: vi.fn(), onError });
    expect(onError).toHaveBeenCalled();
  });
});
