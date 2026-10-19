import type { Health, Session } from "../types";
import type { Banner } from "../uiState";

export function DemoBanner({ health, session }: { health: Health | null; session: Session | null }) {
  const simulated = session?.engine.simulated ?? health?.engine.simulated;
  if (!simulated) return null;
  return (
    <div className="banner banner-demo" role="note">
      <strong>DEMO MODE (simulated).</strong> The patch is scripted, not written by a model. Tests, sandbox and verification are real.
    </div>
  );
}

export function SetupNotice({ health, healthError }: { health: Health | null; healthError: string | null }) {
  const problems = healthError ? [healthError]
    : health ? [health.sandbox, health.ai].filter((c) => !c.ok).map((c) => c.message) : [];
  if (!problems.length) return null;
  return (
    <div className="banner banner-warning" role="alert">
      <strong>Setup needed</strong>
      <ul>{problems.map((p) => <li key={p}>{p}</li>)}</ul>
    </div>
  );
}

export function ErrorBanner({ message, onDismiss }: { message: string | null; onDismiss: () => void }) {
  if (!message) return null;
  return (
    <div className="banner banner-error" role="alert">
      <span>{message}</span>
      <button type="button" className="link" onClick={onDismiss}>Dismiss</button>
    </div>
  );
}

export function OutcomeBanner({ banner }: { banner: Banner | null }) {
  if (!banner) return null;
  return (
    <div className={`outcome outcome-${banner.tone}`} role="status" data-testid="outcome">
      <div className="outcome-title">{banner.tone === "success" ? "✓ " : ""}{banner.title}</div>
      {banner.detail && <div className="outcome-detail">{banner.detail}</div>}
    </div>
  );
}
