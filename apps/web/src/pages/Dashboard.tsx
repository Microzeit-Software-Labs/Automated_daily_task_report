import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type ApprovalRequest, type UiConfig } from "../api";
import { formatDate, formatTime, isOpen, KIND_LABELS, localDateIn } from "../logic";
import { href, navigate } from "../router";
import { ErrorNote, StateChip } from "../ui";

export function Dashboard({ config }: { config: UiConfig }) {
  const today = localDateIn(config.timezone);
  const reviews = useQuery({
    queryKey: ["reviews"],
    queryFn: () => api.reviews({ limit: 20 }),
    refetchInterval: 30_000,
  });
  const start = useMutation({
    mutationFn: api.createManualReview,
    onSuccess: (review) => navigate({ name: "review", id: review.id }),
  });

  const all = reviews.data ?? [];
  const todays = all.filter((r) => r.local_date === today);
  const earlier = all.filter((r) => r.local_date !== today).slice(0, 10);

  return (
    <div className="stack">
      <section className="card">
        <div className="card-head">
          <div>
            <h1>Today</h1>
            <p className="muted">
              {formatDate(today)} · reports open at {config.morning_alert_time.slice(0, 5)} and{" "}
              {config.evening_alert_time.slice(0, 5)} on working days
            </p>
          </div>
          <button className="btn" onClick={() => start.mutate()} disabled={start.isPending}>
            {start.isPending ? "Starting…" : "Start a report now"}
          </button>
        </div>
        {start.error && <ErrorNote error={start.error} />}
        {reviews.error ? (
          <ErrorNote error={reviews.error} />
        ) : reviews.isPending ? (
          <p className="muted">Loading…</p>
        ) : todays.length === 0 ? (
          <p className="empty">
            No reports yet today. The worker opens one at each alert time — or start one now.
          </p>
        ) : (
          <ReviewList reviews={todays} tz={config.timezone} />
        )}
        <AlertsToggle />
      </section>

      {earlier.length > 0 && (
        <section className="card">
          <h2>Earlier</h2>
          <ReviewList reviews={earlier} tz={config.timezone} showDate />
        </section>
      )}

      {config.sheet_url && (
        <p className="muted small">
          Tasks come from your{" "}
          <a href={config.sheet_url} target="_blank" rel="noreferrer">
            Google Sheet
          </a>
          . Edit them there; changes appear here within a minute.
        </p>
      )}
    </div>
  );
}

function ReviewList({ reviews, tz, showDate = false }: { reviews: ApprovalRequest[]; tz: string; showDate?: boolean }) {
  return (
    <ul className="rows">
      {reviews.map((r) => (
        <li key={r.id}>
          <a className={`row ${isOpen(r.state) ? "row-attention" : ""}`} href={href({ name: "review", id: r.id })}>
            <span className="row-main">
              <strong>{KIND_LABELS[r.kind]}</strong>
              <span className="muted small">
                {showDate ? `${formatDate(r.local_date)} · ` : ""}
                {formatTime(r.scheduled_for, tz)} · {r.display_id}
              </span>
            </span>
            <StateChip state={r.state} />
            <span className="row-go" aria-hidden="true">
              {isOpen(r.state) ? "Review →" : "View →"}
            </span>
          </a>
        </li>
      ))}
    </ul>
  );
}

function AlertsToggle() {
  const supported = "Notification" in window;
  const [permission, setPermission] = useState(supported ? Notification.permission : "denied");
  if (!supported || permission === "granted") {
    return permission === "granted" ? (
      <p className="muted small">Desktop alerts are on while this tab is open in the background.</p>
    ) : null;
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
