import { diffStats, parseDiff } from "./diff";
import { DIFF } from "./test/fakeApi";

describe("parseDiff", () => {
  it("numbers old and new lines from the hunk header", () => {
    const rows = parseDiff(DIFF);
    const kinds = rows.map((r) => r.kind);
    expect(kinds).toEqual(["meta", "meta", "hunk", "ctx", "ctx", "del", "add"]);
    const del = rows.find((r) => r.kind === "del")!;
    const add = rows.find((r) => r.kind === "add")!;
    expect([del.oldNo, del.newNo, add.oldNo, add.newNo]).toEqual([21, null, null, 21]);
    expect(rows.filter((r) => r.kind === "ctx").map((r) => [r.oldNo, r.newNo])).toEqual([[19, 19], [20, 20]]);
    expect(add.text).toBe("        return self._gateway.charge(customer_id, amount_cents)");
  });

  it("counts added and removed lines", () => {
    expect(diffStats(parseDiff(DIFF))).toEqual({ added: 1, removed: 1 });
  });

  it("handles the no-newline marker, CRLF and an empty diff", () => {
    const rows = parseDiff("--- a/x\r\n+++ b/x\r\n@@ -1 +1 @@\r\n-a\r\n\\ No newline at end of file\r\n+b\r\n");
    expect(rows.map((r) => r.kind)).toEqual(["meta", "meta", "hunk", "del", "note", "add"]);
    expect(rows[4].text).toBe("No newline at end of file");
    expect(parseDiff("")).toEqual([]);
  });

  it("restarts numbering for a second file and treats blank lines inside a hunk as context", () => {
    const rows = parseDiff("--- a/a\n+++ b/a\n@@ -1,2 +1,2 @@\n x\n\n--- a/b\n+++ b/b\n@@ -5 +5 @@\n-p\n+q\n");
    expect(rows.filter((r) => r.kind === "meta").length).toBe(4);
    const second = rows.filter((r) => r.kind === "del" || r.kind === "add");
    expect([second[0].oldNo, second[1].newNo]).toEqual([5, 5]);
    expect(rows.some((r) => r.kind === "ctx" && r.text === "")).toBe(true);
  });

  it("never throws on garbage", () => {
    expect(() => parseDiff("@@ nonsense @@\n+++\n---\n\u0000")).not.toThrow();
  });
});
