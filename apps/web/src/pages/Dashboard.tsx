import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api, type ReviewListItem, type UiConfig } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { useToast } from "../components/Toast";
import { WhatsAppPill } from "../components/WhatsAppPill";
import {
  deliveryResult,
  formatDate,
  formatTime,
  groupsLabel,
  KIND_LABELS,
  matchesFilter,
  nextReport,
  periodStart,
  REPORT_FILTERS,
  REPORT_PERIODS,
  type ReportFilter,
  type ReportPeriod,
  type ReportResult,
  untilLabel,
  whatsappIsDown,
  workingDaysLabel,
} from "../logic";
import { href, navigate } from "../router";
import { ErrorNote, Icon, ResultChip } from "../ui";
import { useCloseReport } from "../useCloseReport";
import { useNow } from "../useNow";

/** The longest period offered; the list is fetched once for it and the shorter
 * periods are filtered in the browser, so switching is instant. */
const FETCH_DAYS = 30;

interface Row {
  review: ReviewListItem;
  result: ReportResult;
}

export function Dashboard({ config }: { config: UiConfig }) {
  const tz = config.timezone;
  const now = useNow();
  const [filter, setFilter] = useState<ReportFilter>("all");
  const [period, setPeriod] = useState<ReportPeriod>("week");
  const [closing, setClosing] = useState<ReviewListItem | null>(null);
  const toast = useToast();

  const status = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 15_000 });
  const since = periodStart(FETCH_DAYS, tz, now);
  const reviews = useQuery({
    queryKey: ["reviews", since],
    queryFn: () => api.reviews({ since, limit: 100 }),
    refetchInterval: 30_000,
  });
  const start = useMutation({
    mutationFn: api.createManualReview,
    onSuccess: (review) => navigate({ name: "review", id: review.id }),
  });
  const close = useCloseReport();

  const down = whatsappIsDown(status.data);
  const rows = useMemo<Row[]>(
    () =>
      (reviews.data ?? []).map((review) => ({
        review,
        result: deliveryResult(review, review.delivery, { tz, now, whatsappDown: down }),
      })),
    [reviews.data, tz, now, down],
  );

  const waiting = rows.filter((r) => r.result.category === "needs_approval");
  const days = REPORT_PERIODS.find((p) => p.id === period)?.days ?? 7;
  const from = periodStart(days, tz, now);
  const inPeriod = rows.filter((r) => r.review.local_date >= from);
  const shown = inPeriod.filter((r) => matchesFilter(r.result, filter));
  const countFor = (id: ReportFilter) => inPeriod.filter((r) => matchesFilter(r.result, id)).length;

  const next = nextReport(config, now);

  return (
    <div className="page stack">
      <div className="page-head">
        <div>
          <h1>Reports</h1>
          <p className="muted">Review each task report, send it to your WhatsApp groups, and see how it went.</p>
        </div>
      </div>

      <section className="strip" aria-label="Status">
        <div className="strip-cell">
          <span className="strip-label">WhatsApp</span>
          <WhatsAppPill config={config} status={status.data} failed={status.isError} />
          {status.data?.account_number && status.data.state === "CONNECTED" && (
            <span className="muted small">{status.data.account_number}</span>
          )}
        </div>
        <div className="strip-cell">
          <span className="strip-label">Next report</span>
          {next ? (
            <>
              <strong>
                {next.kind === "MORNING" ? "Morning" : "Evening"} report at {formatTime(next.at.toISOString(), tz)}
              </strong>
              <span className="muted small">
                {untilLabel(next.at, tz, now)} · {workingDaysLabel(config.working_days)}
              </span>
            </>
          ) : (
            <span className="muted">No working days set</span>
          )}
        </div>
        <div className="strip-action">
          <button className="btn btn-primary" onClick={() => start.mutate()} disabled={start.isPending}>
            <Icon name="plus" />
            {start.isPending ? "Starting…" : "Start a report now"}
          </button>
        </div>
      </section>
      {start.error && <ErrorNote error={start.error} />}

      {waiting.length > 0 && (
        <section className="card card-attention" aria-labelledby="needs-approval">
          <div className="card-head">
            <h2 id="needs-approval">Needs your approval</h2>
            <span className="count">{waiting.length}</span>
          </div>
          <ul className="approvals">
            {waiting.map(({ review }) => (
              <li key={review.id} className="approval">
                <div className="approval-main">
                  <strong>{KIND_LABELS[review.kind]}</strong>
                  <span className="muted small">
                    {formatDate(review.local_date)} · opened {formatTime(review.scheduled_for, tz)} · {review.display_id}
                  </span>
                </div>
                <div className="approval-actions">
                  <a className="btn btn-primary" href={href({ name: "review", id: review.id })}>
                    Review
                  </a>
                  <button className="btn btn-ghost" onClick={() => setClosing(review)}>
                    Close
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="card" aria-labelledby="history">
        <div className="card-head">
          <h2 id="history">All reports</h2>
          <div className="seg" role="group" aria-label="Period">
            {REPORT_PERIODS.map((p) => (
              <button
                key={p.id}
                className="seg-item"
                aria-pressed={period === p.id}
                onClick={() => setPeriod(p.id)}
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>

        <div className="tabs" role="group" aria-label="Show">
          {REPORT_FILTERS.map((f) => (
            <button key={f.id} className="tab" aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}>
              {f.label}
              <span className="tab-count">{countFor(f.id)}</span>
            </button>
          ))}
        </div>

        {reviews.error ? (
          <ErrorNote error={reviews.error} />
        ) : reviews.isPending ? (
          <p className="muted">Loading…</p>
        ) : shown.length === 0 ? (
          <p className="empty">
            {rows.length === 0
              ? "No reports yet. One opens at each report time on working days, or start one now."
              : "No reports match this view. Try another period or filter."}
          </p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Report</th>
                <th>Time</th>
                <th>Result</th>
                <th>Groups</th>
                <th>
                  <span className="sr-only">Open</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {shown.map(({ review, result }) => (
                <tr key={review.id} className={result.category === "needs_approval" ? "tr-attention" : undefined}>
                  <td data-label="Report">
                    <span className="cell-value">
                      <a className="table-link" href={href({ name: "review", id: review.id })}>
                        {KIND_LABELS[review.kind]}
                      </a>
                      <span className="muted small block">{review.display_id}</span>
                    </span>
                  </td>
                  <td data-label="Time">
                    <span className="cell-value">
                      {formatDate(review.local_date)}
                      <span className="muted small block">{formatTime(review.scheduled_for, tz)}</span>
                    </span>
                  </td>
                  <td data-label="Result">
                    <ResultChip result={result} />
                  </td>
                  <td data-label="Groups" className="cell-groups">
                    {groupsLabel(review.delivery?.group_names)}
                  </td>
                  <td className="cell-go">
                    <a className="btn btn-sm" href={href({ name: "review", id: review.id })}>
                      {result.category === "needs_approval" ? "Review" : "View"}
                      <Icon name="arrow" size={14} />
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <AlertsToggle />

      {closing && (
        <ConfirmDialog
          title="Close this report without sharing?"
          confirmLabel="Close report"
          busy={close.isPending}
          onCancel={() => setClosing(null)}
          onConfirm={() =>
            close.mutate(closing.id, {
              onSuccess: () => {
                setClosing(null);
                toast.show("Report closed without sharing.");
              },
              onError: (error) => {
                setClosing(null);
                toast.show(error.message, "stop");
              },
            })
          }
        >
          <p>
            The {KIND_LABELS[closing.kind].toLowerCase()} for {formatDate(closing.local_date)} won't be sent to any
            group. You can still start a new report any time.
          </p>
        </ConfirmDialog>
      )}
    </div>
  );
}

function AlertsToggle() {
  const supported = "Notification" in window;
  const [permission, setPermission] = useState(supported ? Notification.permission : "denied");
  if (!supported) return null;
  if (permission === "granted") {
    return <p className="muted small">Desktop alerts are on while this tab is open in the background.</p>;
  }
  if (permission === "denied") {
    return <p className="muted small">Desktop alerts are blocked in this browser's site settings.</p>;
  }
  return (
    <p className="muted small">
      <button className="link" onClick={() => void Notification.requestPermission().then(setPermission)}>
        Turn on desktop alerts
      </button>{" "}
      to hear about new reports when this tab is in the background.
    </p>
  );
}
