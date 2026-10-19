import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import App from "./App";
import { FakeApi, SAMPLE, finished, makeSession, waitingSession } from "./test/fakeApi";

// Everything below is content a hostile repository controls: file names, test ids, messages, diffs, descriptions.
const XSS = [
  '<img src=x onerror="window.__pwned=1">',
  "<script>window.__pwned=1</script>",
  '"><svg onload="window.__pwned=1">',
  "<a href=\"javascript:window.__pwned=1\">click</a>",
];

declare global { interface Window { __pwned?: number } }

function assertInert(container: HTMLElement) {
  expect(window.__pwned).toBeUndefined();
  expect(container.querySelector("img[src='x'], script, svg[onload], a[href^='javascript']")).toBeNull();
  expect(container.querySelector("[onerror], [onload], [onclick]")).toBeNull();
}

describe("hostile repository content is shown as text, never as markup", () => {
  beforeEach(() => { window.__pwned = undefined; });

  it("renders hostile file names, messages, descriptions, test ids and diffs inertly", async () => {
    const api = new FakeApi();
    api.repo = { ...SAMPLE, name: XSS[0], files: [`${XSS[1]}.py`, `dir/${XSS[2]}.py`, XSS[3]] };
    const waiting = waitingSession();
    waiting.pending_patch!.diff = ["--- a/x.py", "+++ b/x.py", "@@ -1 +1 @@", `-${XSS[0]}`, `+${XSS[1]}`].join("\n");
    waiting.pending_patch!.summary = XSS[2];
    waiting.pending_patch!.files = [XSS[3]];
    api.session = waiting;
    const user = userEvent.setup();
    const { container } = render(<App api={api} pollMs={0} healthMs={0} />);
    await user.click(await screen.findByRole("button", { name: "Use sample repository" }));
    await screen.findByRole("list", { name: "Repository files" });
    await user.click(screen.getByRole("button", { name: /start debugging/i }));
    api.emitStep(1, "ANALYZE_REPOSITORY", `Located failing test: ${XSS[0]}`);
    await screen.findByRole("button", { name: "Approve Fix" });

    const diff = screen.getByRole("region", { name: "Proposed code change" });
    expect(diff.textContent).toContain(XSS[1]);             // the hostile text is visible...
    expect(within(screen.getByRole("list", { name: "Debugging progress" })).getByText(/Located failing test/).textContent).toContain(XSS[0]);
    assertInert(container);                                // ...but nothing became an element, and nothing ran
  });

  it("renders hostile report fields and error banners inertly", async () => {
    const api = new FakeApi();
    api.session = finished("FAILED", "failed", { message: XSS[0], error_code: XSS[1] });
    api.session.report!.root_cause = { file: XSS[2], line: 1, description: XSS[3] };
    api.session.report!.failing_tests_before = [XSS[0], XSS[1]];
    api.session.report!.attempt_feedback = [{ attempt: 1, patch_sha256: null, reason_code: XSS[2], message: XSS[3] }];
    api.fail.getHealth = undefined;
    window.location.hash = "#s=bbbbbbbbbbbb";
    const { container } = render(<App api={api} pollMs={0} healthMs={0} />);
    await screen.findByTestId("outcome");
    expect(screen.getByTestId("outcome").textContent).toContain(XSS[0]);
    assertInert(container);
  });
});

describe("source guard rails", () => {
  const files = (dir: string): string[] =>
    readdirSync(dir).flatMap((name) => {
      const path = join(dir, name);
      return statSync(path).isDirectory() ? files(path) : /\.(tsx?|css)$/.test(name) && !name.includes(".test.") && !path.includes("/test/") ? [path] : [];
    });
  const source = files(join(__dirname)).map((f) => ({ f, text: readFileSync(f, "utf8") }));

  it.each([
    ["raw HTML injection", /dangerouslySetInnerHTML|\.innerHTML\s*=|insertAdjacentHTML|document\.write/],
    ["dynamic code execution", /\beval\s*\(|new Function\s*\(/],
    ["browser storage (the product rule: nothing persists in the browser)", /\b(localStorage|sessionStorage|indexedDB)\b/],
    ["third-party network calls", /https?:\/\/(?!localhost|127\.0\.0\.1)[a-z0-9.-]+\.[a-z]{2,}/i],
  ]) ("has no %s", (_name: string, pattern: RegExp) => {
    const offenders = source.filter(({ text }) => pattern.test(text)).map(({ f }) => f);
    expect(offenders).toEqual([]);
  });

  it("never links to a user-controlled URL", () => {
    const offenders = source.filter(({ text }) => /<a\s[^>]*href=\{/.test(text)).map(({ f }) => f);
    expect(offenders).toEqual([]);
  });

  it("only ever talks to its own /api", () => {
    const urls = source.flatMap(({ text }) => [...text.matchAll(/["'`](\/[a-z][^"'`\s]*)["'`]/g)].map((m) => m[1]).filter((u) => u.startsWith("/api")));
    expect(urls.length).toBeGreaterThan(5);
    expect(urls.every((u) => u.startsWith("/api/"))).toBe(true);
  });
});

// keep the helper imports "used" for the type checker
void makeSession;
