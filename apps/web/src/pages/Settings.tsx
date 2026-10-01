import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, type SheetSource, type UiConfig, type WhatsAppStatus } from "../api";
import { formatTime, sheetStatus, workingDaysLabel } from "../logic";
import { Chip, ErrorNote } from "../ui";
import { useNow } from "../useNow";

export function Settings({
  config,
  status,
  sheet,
  onLink,
  onChangeSheet,
}: {
  config: UiConfig;
  status: WhatsAppStatus | undefined;
  sheet: SheetSource | undefined;
  onLink: () => void;
  onChangeSheet: () => void;
}) {
  return (
    <div className="page page-narrow stack">
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="muted">How Interlock is connected. The schedule is set in the configuration file.</p>
        </div>
      </div>
      <WhatsAppCard config={config} status={status} onLink={onLink} />
      <SheetCard config={config} sheet={sheet} onChange={onChangeSheet} />
      <ScheduleCard config={config} />
    </div>
  );
}

function SheetCard({
  config,
  sheet,
  onChange,
}: {
  config: UiConfig;
  sheet: SheetSource | undefined;
  onChange: () => void;
}) {
  const now = useNow(30_000);
  const status = sheetStatus(sheet, config.timezone, now);
  const connected = !!sheet?.configured;
  return (
    <section className="card">
      <div className="card-head">
        <div>
          <h2>Google Sheet</h2>
          {connected && sheet.url ? (
            <p className="sheet-url">
              Tasks are read from{" "}
              <a href={sheet.url} target="_blank" rel="noreferrer">
                your Google Sheet
              </a>
              . Edit them there; changes appear here within a minute.
            </p>
          ) : (
            <p className="muted">No sheet is connected, so there are no tasks to report on.</p>
          )}
        </div>
        {sheet?.changeable === false ? null : (
          <button className={connected ? "btn" : "btn btn-primary"} onClick={onChange} disabled={!sheet}>
            {connected ? "Change sheet" : "Connect a sheet"}
          </button>
        )}
      </div>
      <p>
        <Chip tone={status.tone}>{status.title}</Chip>
      </p>
      {status.detail && <p className="field-error">{status.detail}</p>}
      {sheet?.changeable === false && (
        <p className="muted small">
          The two-way Google Sheets sync is turned on and manages the sheet, so the link can't be changed here.
        </p>
      )}
      <p className="muted small">
        Interlock only reads the sheet and never changes it. Rows marked as another person's task are left out of reports.
      </p>
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
