import { useMemo } from "react";
import { type Api, createApi } from "./api";
import { DemoBanner, ErrorBanner, OutcomeBanner, SetupNotice } from "./components/Banners";
import { Header } from "./components/Header";
import { PatchPanel } from "./components/PatchPanel";
import { RepositoryPanel } from "./components/RepositoryPanel";
import { Timeline } from "./components/Timeline";
import { VerificationPanel } from "./components/VerificationPanel";
import { buildTimeline } from "./timeline";
import { isActive, isVerifiedSuccess, outcomeBanner } from "./uiState";
import { useWorkflow } from "./useWorkflow";

interface Props { api?: Api; pollMs?: number; healthMs?: number }

export default function App({ api, pollMs = 2000, healthMs = 10000 }: Props) {
  const client = useMemo(() => api ?? createApi(), [api]);
  const { health, healthError, repo, session, events, pending, error, uiState, actions } =
    useWorkflow(client, { pollMs, healthMs });

  const ready = !!health && health.sandbox.ok && health.ai.ok;
  const notStarted = !session || session.state === "INITIALIZE";   // nothing running yet (or the start failed)
  const finished = !!session && !isActive(session);
  const running = isActive(session) && !notStarted;
  const awaiting = session?.state === "WAITING_APPROVAL" && !!session.pending_patch;
  const canStart = !!repo && ready && pending === null;
  const report = session?.report ?? null;
  const confirmed = isVerifiedSuccess(session);
  const appliedPatch = report?.patch_diff && report.patch_sha256 && !awaiting
    ? { sha256: report.patch_sha256, diff: report.patch_diff, files: report.files_changed } : null;
  const startLabel = pending === "start" ? "Starting..." : finished ? "Run again" : "Start debugging";
  const startHint = !repo ? "Choose a repository first." : !ready ? "Fix the setup problems above first." : "";

  return (
    <div className="app">
      <Header health={health} healthError={healthError} uiState={uiState} />
      <DemoBanner health={health} session={session} />
      <SetupNotice health={health} healthError={healthError} />
      <ErrorBanner message={error} onDismiss={actions.dismissError} />
      <main className="grid">
        <RepositoryPanel repo={repo} pending={pending} locked={running} onSample={actions.useSample} onUpload={actions.upload} />
        <section className="panel" aria-labelledby="session-h">
          <h2 id="session-h">Debugging Session</h2>
          <Timeline rows={buildTimeline(events, session)} waiting={pending === "start"} />
          <div className="actions left">
            {(notStarted || finished) && (
              <button type="button" className="primary" disabled={!canStart} title={startHint}
                      onClick={finished ? () => { actions.reset(); void actions.start(); } : actions.start}>
                {startLabel}
              </button>
            )}
            {running && (
              <button type="button" disabled={pending !== null} onClick={actions.cancel}>
                {pending === "cancel" ? "Cancelling..." : "Cancel"}
              </button>
            )}
          </div>
          {notStarted && startHint && <p className="muted">{startHint}</p>}
          <OutcomeBanner banner={outcomeBanner(session)} />
        </section>
        {awaiting && session?.pending_patch && (
          <div className="wide"><PatchPanel patch={session.pending_patch} awaiting pending={pending} onApprove={actions.approve} onReject={actions.reject} /></div>
        )}
        {appliedPatch && <div className="wide"><PatchPanel patch={appliedPatch} awaiting={false} pending={pending} onApprove={actions.approve} onReject={actions.reject} /></div>}
        {report && report.before && <div className="wide"><VerificationPanel report={report} confirmed={confirmed} /></div>}
      </main>
      <footer className="footer">AI suggestion ≠ verified fix. RepoDoctor reports success only after re-running your tests.</footer>
    </div>
  );
}
