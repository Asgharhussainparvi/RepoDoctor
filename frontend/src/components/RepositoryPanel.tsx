import { useRef, useState } from "react";
import type { Repository } from "../types";
import type { Pending } from "../useWorkflow";

interface Node { name: string; depth: number; folder: boolean }

export function fileTree(files: string[]): Node[] {
  const out: Node[] = [];
  const seen = new Set<string>();
  for (const file of [...files].sort()) {
    const parts = file.split("/");
    parts.forEach((name, i) => {
      const path = parts.slice(0, i + 1).join("/");
      if (seen.has(path)) return;
      seen.add(path);
      out.push({ name, depth: i, folder: i < parts.length - 1 });
    });
  }
  return out;
}

interface Props {
  repo: Repository | null; pending: Pending; locked: boolean;
  onSample: () => void; onUpload: (file: File) => void;
}

export function RepositoryPanel({ repo, pending, locked, onSample, onUpload }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const uploading = pending === "upload" || pending === "sample";
  const disabled = uploading || locked;
  return (
    <section className="panel" aria-labelledby="repo-h">
      <h2 id="repo-h">Repository</h2>
      <div
        className={`dropzone${over ? " over" : ""}`}
        onDragOver={(e) => { e.preventDefault(); if (!disabled) setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); const f = e.dataTransfer.files?.[0]; if (f && !disabled) onUpload(f); }}
      >
        <p>Drop a <code>.zip</code> of a Python repository here</p>
        <div className="row">
          <button type="button" disabled={disabled} onClick={() => input.current?.click()}>
            {pending === "upload" ? "Uploading..." : "Choose .zip"}
          </button>
          <button type="button" disabled={disabled} onClick={onSample}>
            {pending === "sample" ? "Preparing..." : "Use sample repository"}
          </button>
        </div>
        <input ref={input} type="file" accept=".zip" hidden aria-label="Repository zip file" data-testid="file-input"
               onChange={(e) => { const f = e.target.files?.[0]; if (f) onUpload(f); e.target.value = ""; }} />
      </div>
      {uploading && <p className="muted" aria-live="polite"><span className="spinner" aria-hidden="true" /> Preparing repository...</p>}
      {repo && (
        <>
          <p className="repo-name"><strong>{repo.name}</strong> <span className="muted">{repo.file_count} files{repo.kind === "sample" ? " (sample)" : ""}</span></p>
          <ul className="tree" aria-label="Repository files">
            {fileTree(repo.files).map((n) => (
              <li key={`${n.depth}:${n.name}:${n.folder}`} style={{ paddingLeft: `${n.depth * 14}px` }}>
                <span aria-hidden="true">{n.folder ? "📁" : "📄"}</span> {n.name}{n.folder ? "/" : ""}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
