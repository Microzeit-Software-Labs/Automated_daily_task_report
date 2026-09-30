import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, ApiError, type Group, type ReviewDetail, type Share, type UiConfig } from "../api";
import {
  defaultRecipientIds,
  formatDate,
  formatTime,
  isOpen,
  isSettling,
  KIND_LABELS,
  resolveSendAt,
  shareButtonLabel,
  type SendChoice,
  whatsappSegments,
} from "../logic";
import { href } from "../router";
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
        <Composer detail={d} groups={groups.data ?? []} groupsError={groups.error} config={config} />
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
  groups,
  groupsError,
  config,
}: {
  detail: ReviewDetail;
  groups: Group[];
  groupsError: unknown;
  config: UiConfig;
}) {
  const queryClient = useQueryClient();
  const id = detail.request.id;
  const preview = useQuery({
    queryKey: ["preview", id],
    queryFn: () => api.preview(id),
    // Follows the sheet import, which can change tasks every minute. The
    // commit carries this preview's hash, so what's on screen is what sends.
    refetchInterval: 60_000,
  });
  const status = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 15_000 });

  const [changedNotice, setChangedNotice] = useState(false);
  const lastHash = useRef<string | null>(null);
  useEffect(() => {
    const hash = preview.data?.content_hash;
    if (!hash) return;
    if (lastHash.current !== null && lastHash.current !== hash) setChangedNotice(true);
    lastHash.current = hash;
  }, [preview.data?.content_hash]);

  const enabled = groups.filter((g) => g.enabled);
  const [selected, setSelected] = useState<Set<string> | null>(null);
  useEffect(() => {
    if (selected === null && groups.length > 0) {
      setSelected(new Set(defaultRecipientIds(detail.request.kind, groups)));
    }
  }, [groups, selected, detail.request.kind]);
  const chosen = enabled.filter((g) => selected?.has(g.id));

  const [when, setWhen] = useState<SendChoice>({ kind: "now" });
  const sendAt = resolveSendAt(when, config.timezone);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["review", id] });
    void queryClient.invalidateQueries({ queryKey: ["reviews"] });
  };

  const share = useMutation({
    mutationFn: () => {
      if (!preview.data) throw new Error("The preview hasn't loaded yet.");
      if ("error" in sendAt) throw new Error(sendAt.error);
      return api.commit(
        id,
        {
          mode: "SHARE_ONLY",
          action_version: detail.shares.length + 1,
          recipient_group_ids: chosen.map((g) => g.id),
          send_at: sendAt.sendAt,
          expected_content_hash: preview.data.content_hash,
        },
        crypto.randomUUID(),
      );
    },
    onSuccess: refresh,
    onError: (error) => {
      if (error instanceof ApiError && error.code === "DATASET_VERSION_CONFLICT") {
        void preview.refetch();
      }
    },
  });

  const skip = useMutation({
    mutationFn: () =>
      api.commit(id, { mode: "UPDATE_ONLY", action_version: 1 }, crypto.randomUUID()),
    onSuccess: refresh,
  });

  const busy = share.isPending || skip.isPending;
  const canShare = !!preview.data && chosen.length > 0 && !("error" in sendAt) && !busy;
  const drifted = share.error instanceof ApiError && share.error.code === "DATASET_VERSION_CONFLICT";

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
            <button className="link" onClick={() => { setChangedNotice(false); share.reset(); }}>
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
                  checked={selected?.has(g.id) ?? false}
                  onChange={(e) => {
                    const next = new Set(selected ?? []);
                    if (e.target.checked) next.add(g.id);
                    else next.delete(g.id);
                    setSelected(next);
                  }}
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
          <button className="btn btn-primary" disabled={!canShare} onClick={() => share.mutate()}>
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

/** The message as WhatsApp will show it: the table image (if any) with the
 * text as its caption, or just the text. */
function Report({ text, imageUrl }: { text: string; imageUrl: string | null }) {
  if (!imageUrl) return <Bubble text={text} />;
  return (
    <div className="bubble bubble-image">
      <a href={imageUrl} target="_blank" rel="noreferrer" title="Open full size">
        <img src={imageUrl} alt="Report table that will be sent" className="report-image" />
      </a>
      <Bubble text={text} bare />
    </div>
  );
}

function Bubble({ text, bare = false }: { text: string; bare?: boolean }) {
  return (
    <div className={bare ? "bubble-caption" : "bubble"} aria-label="WhatsApp message preview">
      {text.split("\n").map((line, i) => (
        <div key={i} className="bubble-line">
          {line === ""
            ? " "
            : whatsappSegments(line).map((seg, j) =>
                seg.bold ? <strong key={j}>{seg.text}</strong> : seg.italic ? <em key={j}>{seg.text}</em> : <span key={j}>{seg.text}</span>,
              )}
        </div>
      ))}
    </div>
  );
}
