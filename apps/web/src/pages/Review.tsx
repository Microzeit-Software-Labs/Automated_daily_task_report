import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api, type Group, type ReviewDetail, type Share, type UiConfig } from "../api";
import {
  formatDate,
  formatTime,
  isOpen,
  isSettling,
  KIND_LABELS,
  shareButtonLabel,
} from "../logic";
import { href } from "../router";
import { Report } from "../components/ReportPreview";
import { useShare } from "../useShare";
import { ErrorNote, StateChip } from "../ui";

export function Review({ id, config }: { id: string; config: UiConfig }) {
  const queryClient = useQueryClient();
  const detail = useQuery({
    queryKey: ["review", id],
    queryFn: () => api.review(id),
    // Poll only while a send is still in flight.
    refetchInterval: (q) => (q.state.data?.shares.some(isSettling) ? 3_000 : false),
  });
  const groups = useQuery({ queryKey: ["groups"], queryFn: api.groups });

  // Record that the user opened it (REVIEW_PENDING -> USER_EDITING). Once.
  const opened = useRef(false);
  const state = detail.data?.request.state;
  useEffect(() => {
    if (state !== "REVIEW_PENDING" || opened.current) return;
    opened.current = true;
    void api.openReview(id).then(() => queryClient.invalidateQueries({ queryKey: ["reviews"] }));
  }, [state, id, queryClient]);

  if (detail.error) return <ErrorNote error={detail.error} />;
  if (!detail.data) return <p className="muted">Loading…</p>;
  const d = detail.data;
  const r = d.request;

  return (
    <div className="stack">
      <a className="back" href={href({ name: "dashboard" })}>
        ← All reports
      </a>
      <section className="card">
        <div className="card-head">
          <div>
            <h1>{KIND_LABELS[r.kind]}</h1>
            <p className="muted">
              {formatDate(r.local_date)} · opened {formatTime(r.scheduled_for, config.timezone)} · {r.display_id}
            </p>
          </div>
          <StateChip state={r.state} />
        </div>
        <WhatHappened detail={d} />
      </section>

      {isOpen(r.state) ? (
        <Composer detail={d} groupsError={groups.error} config={config} />
      ) : d.shares.length > 0 ? (
        d.shares.map((share) => (
          <Delivery key={share.job.id} share={share} groups={groups.data ?? []} reviewId={id} config={config} />
        ))
      ) : (
        <section className="card">
          <p className="empty">
            {r.state === "CLOSED_NO_SHARE"
              ? "You closed this report without sharing it."
              : `This report is ${r.state.toLowerCase().replaceAll("_", " ")} and was not shared.`}
          </p>
        </section>
      )}
    </div>
  );
}

// -- 1. What happened ------------------------------------------------------

function WhatHappened({ detail }: { detail: ReviewDetail }) {
  const s = detail.summary;
  const c = detail.changes_since_last_share;
  const tiles: [string, number, string][] = [
    ["Completed today", s.completed_today, "go"],
    ["In progress", s.in_progress, "info"],
    ["Pending", s.pending, "warn"],
    ["Overdue", s.overdue, s.overdue > 0 ? "stop" : "neutral"],
    ["Blocked", s.blocked, "neutral"],
  ];
  return (
    <>
      <div className="tiles">
        {tiles.map(([label, value, tone]) => (
          <div key={label} className={`tile tile-${tone}`}>
            <span className="tile-value">{value}</span>
            <span className="tile-label">{label}</span>
          </div>
        ))}
      </div>
      <p className="muted small">
        Since the last shared report: {c.added} added · {c.completed} completed · {c.modified} changed
      </p>
    </>
  );
}

// -- 2-4. What, who, when --------------------------------------------------

function Composer({
  detail,
  groupsError,
  config,
}: {
  detail: ReviewDetail;
  groupsError: unknown;
  config: UiConfig;
}) {
  const id = detail.request.id;
  const {
    enabled,
    chosen,
    isSelected,
    toggle,
    preview,
    status,
    changedNotice,
    dismissChanged,
    drifted,
    when,
    setWhen,
    sendAt,
    share,
    skip,
    busy,
    ready,
  } = useShare({ review: detail.request, config, priorShares: detail.shares.length });
  const canShare = ready && !("error" in sendAt);

  return (
    <>
      <section className="card">
        <div className="card-head">
          <h2>What will be shared</h2>
          <button className="btn btn-quiet" onClick={() => void preview.refetch()} disabled={preview.isFetching}>
            {preview.isFetching ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        {(changedNotice || drifted) && (
          <div className="note note-warn" role="status">
            <strong>Your tasks changed</strong>
            <span>The message below has been updated. Read it again before you share.</span>
            <button className="link" onClick={dismissChanged}>
              Got it
            </button>
          </div>
        )}
        {preview.error ? (
          <ErrorNote error={preview.error} />
        ) : preview.data ? (
          <Report
            text={preview.data.rendered_body}
            // The hash changes with the data, so a stale image is never reused.
            imageUrl={
              preview.data.has_image
                ? `/approval-requests/${id}/preview.png?h=${preview.data.content_hash}&t=${preview.dataUpdatedAt}`
                : null
            }
          />
        ) : (
          <p className="muted">Rendering…</p>
        )}
        <p className="muted small">
          This is exactly what will be sent. Approving freezes it; a later sheet edit won't change what goes out.
        </p>
      </section>

      <section className="card">
        <h2>Who receives it</h2>
        {groupsError ? (
          <ErrorNote error={groupsError} />
        ) : enabled.length === 0 ? (
          <p className="empty">
            No WhatsApp groups yet. <a href={href({ name: "groups" })}>Add a group</a> first.
          </p>
        ) : (
          <div className="checks">
            {enabled.map((g) => (
              <label key={g.id} className="check">
                <input
                  type="checkbox"
                  checked={isSelected(g.id)}
                  onChange={(e) => toggle(g.id, e.target.checked)}
                />
                <span>
                  {g.display_name}
                  {g.description && <span className="muted small"> · {g.description}</span>}
                </span>
              </label>
            ))}
          </div>
        )}

        <h2 className="spaced">When</h2>
        <div className="segmented" role="radiogroup" aria-label="When to send">
          <label className="check">
            <input type="radio" name="when" checked={when.kind === "now"} onChange={() => setWhen({ kind: "now" })} />
            Now
          </label>
          <label className="check">
            <input type="radio" name="when" checked={when.kind === "in5"} onChange={() => setWhen({ kind: "in5" })} />
            In 5 minutes
          </label>
          {config.allow_custom_send_time && (
            <label className="check">
              <input
                type="radio"
                name="when"
                checked={when.kind === "at"}
                onChange={() => setWhen({ kind: "at", time: "" })}
              />
              Later today at
              <input
                type="time"
                className="input input-time"
                value={when.kind === "at" ? when.time : ""}
                onChange={(e) => setWhen({ kind: "at", time: e.target.value })}
                onFocus={() => when.kind !== "at" && setWhen({ kind: "at", time: "" })}
              />
            </label>
          )}
        </div>
        {"error" in sendAt && when.kind === "at" && when.time !== "" && (
          <p className="field-error">{sendAt.error}</p>
        )}

        {status.data && !status.data.can_send && (
          <div className="note note-warn" role="status">
            <strong>WhatsApp isn't connected</strong>
            <span>
              {status.data.detail || status.data.state}. You can still approve; sending will retry and may fail
              until the agent is back.
            </span>
          </div>
        )}
        {share.error && !drifted && <ErrorNote error={share.error} />}
        {skip.error && <ErrorNote error={skip.error} />}

        <div className="actions">
          <button
            className="btn btn-quiet"
            disabled={busy}
            onClick={() => {
              if (window.confirm("Close this report without sharing it?")) skip.mutate();
            }}
          >
            Don't share this one
          </button>
          <button className="btn btn-primary" disabled={!canShare} onClick={() => share.mutate(when)}>
            {share.isPending ? "Sharing…" : shareButtonLabel(chosen.length)}
          </button>
        </div>
        {chosen.length > 0 && (
          <p className="muted small right">
            To {chosen.map((g) => g.display_name).join(", ")}
            {when.kind === "now" ? ", right away" : ""}
          </p>
        )}
      </section>
    </>
  );
}

// -- 5. Did it send --------------------------------------------------------

const DEFER_TEXT: Record<string, string> = {
  CROSSED_DAY_BOUNDARY: "it was approved on a different day",
  SNAPSHOT_STALE: "it waited longer than the grace window",
  DATA_DRIFTED: "your tasks changed after you approved it",
};

function Delivery({
  share,
  groups,
  reviewId,
  config,
}: {
  share: Share;
  groups: Group[];
  reviewId: string;
  config: UiConfig;
}) {
  const queryClient = useQueryClient();
  const names = new Map(groups.map((g) => [g.id, g.display_name]));
  const retry = useMutation({
    mutationFn: api.retryRecipient,
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["review", reviewId] }),
  });
  const job = share.job;

  return (
    <section className="card">
      <div className="card-head">
        <div>
          <h2>Delivery</h2>
          <p className="muted small">
            {job.display_id} ·{" "}
            {job.sent_at
              ? `sent ${formatTime(job.sent_at, config.timezone)}`
              : `scheduled for ${formatTime(share.run_at, config.timezone)}`}
          </p>
        </div>
        <StateChip state={job.state} />
      </div>
      {job.state === "DEFERRED" && (
        <div className="note note-warn">
          <strong>Held back</strong>
          <span>
            Not sent because {DEFER_TEXT[job.deferred_reason ?? ""] ?? job.deferred_reason}. Start a new report to
            share current data.
          </span>
        </div>
      )}
      <ul className="rows">
        {share.recipients.map((r) => (
          <li key={r.id} className="row row-static">
            <span className="row-main">
              <strong>{names.get(r.whatsapp_group_id) ?? r.whatsapp_group_id}</strong>
              {r.error_detail && <span className="muted small">{r.error_detail}</span>}
            </span>
            {r.attempts > 1 && <span className="muted small">{r.attempts} attempts</span>}
            <StateChip state={r.state} />
            {r.state === "FAILED" && (
              <button className="btn btn-quiet" disabled={retry.isPending} onClick={() => retry.mutate(r.id)}>
                Retry
              </button>
            )}
          </li>
        ))}
      </ul>
      {retry.error && <ErrorNote error={retry.error} />}
      <details className="sent-body">
        <summary>The message {job.state === "SENT" ? "that was sent" : "being sent"}</summary>
        <Report
          text={share.rendered_body}
          imageUrl={share.has_image ? `/shares/snapshots/${share.snapshot_id}/image.png` : null}
        />
      </details>
    </section>
  );
}
