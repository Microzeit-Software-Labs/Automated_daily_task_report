import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, type UiConfig, type WhatsAppStatus } from "../api";
import { formatTime } from "../logic";
import { ErrorNote } from "../ui";

export function Settings({
  config,
  status,
  onLink,
}: {
  config: UiConfig;
  status: WhatsAppStatus | undefined;
  onLink: () => void;
}) {
  return (
    <div className="stack">
      <h1>Settings</h1>
      <WhatsAppCard config={config} status={status} onLink={onLink} />
    </div>
  );
}

function WhatsAppCard({
  config,
  status,
  onLink,
}: {
  config: UiConfig;
  status: WhatsAppStatus | undefined;
  onLink: () => void;
}) {
  const queryClient = useQueryClient();
  const reconnect = useMutation({
    mutationFn: api.reconnect,
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["status"] }),
  });

  if (!status) {
    return (
      <section className="card">
        <h2>WhatsApp</h2>
        <p className="muted">Checking…</p>
      </section>
    );
  }
  if (status.provider === "mock") {
    return (
      <section className="card">
        <h2>WhatsApp</h2>
        <p className="muted">
          Test mode: no real WhatsApp is connected (<code>WHATSAPP_PROVIDER=mock</code>). Messages are recorded, not
          sent.
        </p>
      </section>
    );
  }

  const connected = status.state === "CONNECTED";
  const canReconnect = status.reason === "REPLACED";
  return (
    <section className="card">
      <div className="card-head">
        <div>
          <h2>WhatsApp</h2>
          <p className={connected ? "" : "muted"}>
            {connected
              ? `Connected${status.account_number ? ` as ${status.account_number}` : ""}${status.account_name ? ` (${status.account_name})` : ""}`
              : "Not connected"}
          </p>
          {!connected && status.account_number && (
            <p className="muted small">Last connected as {status.account_number}</p>
          )}
          <p className="muted small">
            Last report sent: {status.last_successful_send_at ? formatTime(status.last_successful_send_at, config.timezone) : "none yet"}
          </p>
        </div>
        <div className="actions inline">
          {canReconnect && (
            <button className="btn" disabled={reconnect.isPending} onClick={() => reconnect.mutate()}>
              {reconnect.isPending ? "Reconnecting…" : "Reconnect here"}
            </button>
          )}
          <button className={connected ? "btn" : "btn btn-primary"} onClick={onLink}>
            {connected ? "Link a different phone" : "Reconnect WhatsApp"}
          </button>
        </div>
      </div>
      {reconnect.error && <ErrorNote error={reconnect.error} />}
      <p className="muted small">
        Linking works like WhatsApp Web: scan a QR code from <strong>Settings → Linked Devices → Link a Device</strong>{" "}
        on the phone. The phone doesn't need to stay nearby afterwards.
      </p>
    </section>
  );
}
