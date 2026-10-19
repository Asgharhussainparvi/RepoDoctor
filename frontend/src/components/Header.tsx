import type { Health, UiState } from "../types";
import { STATE_LABEL } from "../uiState";

interface Props { health: Health | null; healthError: string | null; uiState: UiState }

export function Header({ health, healthError, uiState }: Props) {
  const status = healthError ? { tone: "bad", text: "Server offline" }
    : !health ? { tone: "wait", text: "Connecting..." }
    : health.status === "ok" ? { tone: "good", text: "Ready" } : { tone: "warn", text: "Needs setup" };
  return (
    <header className="header">
      <h1>RepoDoctor <span className="tagline">autonomous debugging, verified</span></h1>
      <div className="header-right">
        <span className={`state-badge state-${uiState.replace(/\s+/g, "-")}`} aria-label="Session state">{STATE_LABEL[uiState]}</span>
        <span className={`pill pill-${status.tone}`} role="status"><span className="dot" aria-hidden="true" /> {status.text}</span>
      </div>
    </header>
  );
}
