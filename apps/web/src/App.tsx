import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type UiConfig, type WhatsAppStatus } from "./api";
import { PromptHost } from "./components/PromptHost";
import { WhatsAppBanner } from "./components/WhatsAppBanner";
import { WhatsAppLinkModal } from "./components/WhatsAppLinkModal";
import { formatTime } from "./logic";
import { Dashboard } from "./pages/Dashboard";
import { Groups } from "./pages/Groups";
import { Review } from "./pages/Review";
import { Settings } from "./pages/Settings";
import { href, useRoute } from "./router";
import { ErrorNote } from "./ui";

export function App() {
  const route = useRoute();
  const config = useQuery({ queryKey: ["config"], queryFn: api.config, staleTime: Infinity });
  // One shared status query: the pill, the banner and Settings all read it.
  const status = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 15_000 });
  const [linking, setLinking] = useState(false);

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href={href({ name: "dashboard" })}>
          Interlock
        </a>
        <nav className="nav">
          <a
            href={href({ name: "dashboard" })}
            aria-current={route.name === "dashboard" || route.name === "review" ? "page" : undefined}
          >
            Reports
          </a>
          <a href={href({ name: "groups" })} aria-current={route.name === "groups" ? "page" : undefined}>
            Groups
          </a>
          <a href={href({ name: "settings" })} aria-current={route.name === "settings" ? "page" : undefined}>
            Settings
          </a>
        </nav>
        {config.data && <StatusPill config={config.data} status={status.data} failed={status.isError} />}
      </header>
      <WhatsAppBanner status={status.data} onLink={() => setLinking(true)} />
      <main className="main">
        {config.error ? (
          <ErrorNote error={config.error} />
        ) : !config.data ? (
          <p className="muted">Loading…</p>
        ) : route.name === "review" ? (
          <Review id={route.id} config={config.data} />
        ) : route.name === "groups" ? (
          <Groups />
        ) : route.name === "settings" ? (
          <Settings config={config.data} status={status.data} onLink={() => setLinking(true)} />
        ) : (
          <Dashboard config={config.data} />
        )}
      </main>
      {config.data && <PromptHost config={config.data} />}
      {linking && <WhatsAppLinkModal status={status.data} onClose={() => setLinking(false)} />}
    </div>
  );
}

const STATE_LABELS: Record<string, string> = {
  CONNECTED: "WhatsApp connected",
  CONNECTING: "WhatsApp connecting",
  LOGIN_REQUIRED: "WhatsApp needs linking",
  UNAVAILABLE: "WhatsApp unavailable",
  AUTOMATION_ERROR: "WhatsApp error",
};

function StatusPill({
  config,
  status: s,
  failed,
}: {
  config: UiConfig;
  status: WhatsAppStatus | undefined;
  failed: boolean;
}) {
  // The mock always reports CONNECTED; never let it pass for real WhatsApp.
  const mock = s?.provider === "mock";
  const tone = !s
    ? "neutral"
    : mock
      ? "warn"
      : s.can_send
        ? "go"
        : s.state === "CONNECTING"
          ? "warn"
          : "stop";
  const label = failed
    ? "API unreachable"
    : mock
      ? "Test mode: WhatsApp not connected (WHATSAPP_PROVIDER=mock)"
      : s
        ? s.reason === "AGENT_OFFLINE"
          ? "WhatsApp service stopped"
          : (STATE_LABELS[s.state] ?? s.state)
        : "Checking…";
  const title = s
    ? `${s.provider} · ${s.detail || s.state} · last send ${formatTime(s.last_successful_send_at, config.timezone)}`
    : undefined;
  return (
    <span className={`pill pill-${failed ? "stop" : tone}`} title={title} role="status">
      <span className="dot" aria-hidden="true" />
      {label}
    </span>
  );
}
