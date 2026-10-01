import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, type UiConfig, type WhatsAppStatus } from "../api";
import { formatTime, workingDaysLabel } from "../logic";
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
    <div className="page page-narrow stack">
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="muted">How Interlock is connected. The sheet and schedule are set in the configuration file.</p>
        </div>
      </div>
      <WhatsAppCard config={config} status={status} onLink={onLink} />
      <SheetCard config={config} />
      <ScheduleCard config={config} />
    </div>
  );
}

function SheetCard({ config }: { config: UiConfig }) {
  return (
    <section className="card">
      <h2>Google Sheet</h2>
      {config.sheet_url ? (
        <>
          <p>
            Tasks are read from{" "}
            <a href={config.sheet_url} target="_blank" rel="noreferrer">
              your Google Sheet
            </a>
            . Edit them there; changes appear here within a minute.
          </p>
          <p className="muted small">
            Interlock only reads the sheet and never changes it. Rows marked as another person's task are left out of
            reports.
          </p>
        </>
      ) : (
        <p className="muted">
          No sheet is connected, so reports use the tasks stored in Interlock. To import from a Google Sheet, set{" "}
          <code>SHEET_IMPORT_URL</code> (see docs/sheet-import-setup.md).
        </p>
      )}
    </section>
  );
}

function ScheduleCard({ config }: { config: UiConfig }) {
  const time = (value: string) => value.slice(0, 5);
  return (
    <section className="card">
      <h2>Schedule</h2>
      <dl className="facts">
        <div>
          <dt>Morning report</dt>
          <dd>{time(config.morning_alert_time)}</dd>
        </div>
        <div>
          <dt>Evening report</dt>
          <dd>{time(config.evening_alert_time)}</dd>
        </div>
        <div>
          <dt>Days</dt>
          <dd>{workingDaysLabel(config.working_days)}</dd>
        </div>
        <div>
          <dt>Time zone</dt>
          <dd>{config.timezone}</dd>
        </div>
      </dl>
      <p className="muted small">
        A report opens at each time on those days and waits for your approval. Nothing is sent until you approve it.
      </p>
    </section>
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
