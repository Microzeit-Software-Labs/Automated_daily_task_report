import { useQuery } from "@tanstack/react-query";

import { api, type UiConfig, type WhatsAppStatus } from "./api";
import { formatTime } from "./logic";
import { Dashboard } from "./pages/Dashboard";
import { Groups } from "./pages/Groups";
import { Review } from "./pages/Review";
import { href, useRoute } from "./router";
import { ErrorNote } from "./ui";

export function App() {
  const route = useRoute();
  const config = useQuery({ queryKey: ["config"], queryFn: api.config, staleTime: Infinity });

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href={href({ name: "dashboard" })}>
          Interlock
        </a>
        <nav className="nav">
          <a href={href({ name: "dashboard" })} aria-current={route.name !== "groups" ? "page" : undefined}>
            Reports
          </a>
          <a href={href({ name: "groups" })} aria-current={route.name === "groups" ? "page" : undefined}>
            Groups
          </a>
        </nav>
        {config.data && <StatusPill config={config.data} />}
      </header>
      <main className="main">
        {config.error ? (
          <ErrorNote error={config.error} />
        ) : !config.data ? (
          <p className="muted">Loading…</p>
        ) : route.name === "review" ? (
          <Review id={route.id} config={config.data} />
        ) : route.name === "groups" ? (
          <Groups />
        ) : (
          <Dashboard config={config.data} />
        )}
      </main>
    </div>
  );
}

const STATE_LABELS: Record<string, string> = {
  CONNECTED: "WhatsApp connected",
  CONNECTING: "WhatsApp connecting",
  LOGIN_REQUIRED: "WhatsApp needs pairing",
  UNAVAILABLE: "WhatsApp agent offline",
  AUTOMATION_ERROR: "WhatsApp error",
};

function StatusPill({ config }: { config: UiConfig }) {
  const status = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 15_000 });
  const s: WhatsAppStatus | undefined = status.data;
  // The mock always reports CONNECTED; never let it pass for real WhatsApp.
  const mock = s?.provider === "mock";
  const tone = !s ? "neutral" : mock ? "warn" : s.can_send ? "go" : s.state === "CONNECTING" ? "warn" : "stop";
  const label = status.error
    ? "API unreachable"
    : mock
      ? "Test mode: WhatsApp not connected (WHATSAPP_PROVIDER=mock)"
      : s
        ? (STATE_LABELS[s.state] ?? s.state)
        : "Checking…";
  const title = s
    ? `${s.provider} · ${s.detail || s.state} · last send ${formatTime(s.last_successful_send_at, config.timezone)}`
    : undefined;
  return (
    <span className={`pill pill-${status.error ? "stop" : tone}`} title={title} role="status">
      <span className="dot" aria-hidden="true" />
      {label}
    </span>
  );
}
