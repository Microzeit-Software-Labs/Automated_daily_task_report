import type { UiConfig, WhatsAppStatus } from "../api";
import { formatTime } from "../logic";

const STATE_LABELS: Record<string, string> = {
  CONNECTED: "WhatsApp connected",
  CONNECTING: "WhatsApp connecting",
  LOGIN_REQUIRED: "WhatsApp needs linking",
  UNAVAILABLE: "WhatsApp unavailable",
  AUTOMATION_ERROR: "WhatsApp error",
};

/** The one-glance WhatsApp state, in the header and on the Reports page. */
export function WhatsAppPill({
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
