import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, type Group, type ReviewDetail, type Share, type UiConfig } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Report } from "../components/ReportPreview";
import { useToast } from "../components/Toast";
import {
  formatDate,
  formatTime,
  isOpen,
  isSettling,
  KIND_LABELS,
  reportBanner,
  shareButtonLabel,
  whatsappIsDown,
} from "../logic";
import { href } from "../router";
import { Callout, ErrorNote, Icon, StateChip } from "../ui";
import { useNow } from "../useNow";
import { useShare } from "../useShare";

export function Review({ id, config }: { id: string; config: UiConfig }) {
  const queryClient = useQueryClient();
  const now = useNow();
  const detail = useQuery({
    queryKey: ["review", id],
    queryFn: () => api.review(id),
    // Poll only while a send is still in flight.
    refetchInterval: (q) => (q.state.data?.shares.some(isSettling) ? 3_000 : false),
  });
  const groups = useQuery({ queryKey: ["groups"], queryFn: api.groups });
  const status = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 15_000 });

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
  const shares = [...d.shares].sort((a, b) => b.job.action_version - a.job.action_version); // newest first
  const banner = reportBanner(r, shares[0], { tz: config.timezone, now, whatsappDown: whatsappIsDown(status.data) });

  return (
    <div className="page stack">
      <a className="back" href={href({ name: "dashboard" })}>
        ← All reports
      </a>
      <div className="page-head">
        <div>
          <h1>{KIND_LABELS[r.kind]}</h1>
          <p className="muted">
            {formatDate(r.local_date)} · opened {formatTime(r.scheduled_for, config.timezone)} · {r.display_id}
          </p>
        </div>
      </div>

      <Callout tone={banner.tone} title={banner.title}>
        {banner.message}
      </Callout>

      {isOpen(r.state) ? (
        <Composer detail={d} groupsError={groups.error} config={config} />
      ) : shares.length > 0 ? (
        <Sent shares={shares} groups={groups.data ?? []} reviewId={id} config={config} />
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

// -- Waiting for approval: the message on the left, the decision on the right -----

function Counts({ detail }: { detail: ReviewDetail }) {
  const s = detail.summary;
  const c = detail.changes_since_last_share;
  const items: [string, number, string][] = [
    ["Completed today", s.completed_today, "go"],
    ["In progress", s.in_progress, "info"],
    ["Pending", s.pending, "warn"],
    ["Overdue", s.overdue, s.overdue > 0 ? "stop" : "neutral"],
    ["Blocked", s.blocked, "neutral"],
  ];
  return (
    <>
      <dl className="counts">
        {items.map(([label, value, tone]) => (
          <div key={label} className={`count count-${tone}`}>
            <dd>{value}</dd>
            <dt>{label}</dt>
          </div>
        ))}
      </dl>
      <p className="muted small">
        Since the last shared report: {c.added} added · {c.completed} completed · {c.modified} changed
      </p>
    </>
  );
}

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
  const toast = useToast();
  const [confirmClose, setConfirmClose] = useState(false);
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
    <div className="split">
      <section className="card split-main">
        <div className="card-head">
          <h2>What will be shared</h2>
          <button className="btn btn-ghost btn-sm" onClick={() => void preview.refetch()} disabled={preview.isFetching}>
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

      <aside className="panel split-side" aria-label="Share this report">
        <Counts detail={detail} />

        <h3>Who receives it</h3>
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
                <input type="checkbox" checked={isSelected(g.id)} onChange={(e) => toggle(g.id, e.target.checked)} />
                <span>
                  {g.display_name}
                  {g.description && <span className="muted small"> · {g.description}</span>}
                </span>
              </label>
            ))}
          </div>
        )}

        <h3>When</h3>
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
        {"error" in sendAt && when.kind === "at" && when.time !== "" && <p className="field-error">{sendAt.error}</p>}

        {status.data && !status.data.can_send && (
          <div className="note note-warn" role="status">
            <strong>WhatsApp isn't connected</strong>
            <span>
              {status.data.detail || status.data.state}. You can still approve: it waits and goes out when WhatsApp is
              back, if it is still the same day.
            </span>
          </div>
        )}
        {share.error && !drifted && <ErrorNote error={share.error} />}
        {skip.error && <ErrorNote error={skip.error} />}

        <button className="btn btn-primary btn-block" disabled={!canShare} onClick={() => share.mutate(when)}>
          <Icon name="send" />
          {share.isPending ? "Sharing…" : shareButtonLabel(chosen.length)}
        </button>
        {chosen.length > 0 && (
          <p className="muted small">
            To {chosen.map((g) => g.display_name).join(", ")}
            {when.kind === "now" ? ", right away" : ""}
          </p>
        )}
        <button className="btn btn-ghost btn-block" disabled={busy} onClick={() => setConfirmClose(true)}>
          Don't share this one
        </button>
      </aside>

      {confirmClose && (
        <ConfirmDialog
          title="Close this report without sharing?"
          confirmLabel="Close report"
          busy={skip.isPending}
          onCancel={() => setConfirmClose(false)}
          onConfirm={() =>
            skip.mutate(undefined, {
              onSuccess: () => {
                setConfirmClose(false);
                toast.show("Report closed without sharing.");
              },
              onError: () => setConfirmClose(false),
            })
          }
        >
          <p>It won't be sent to any group. You can still start a new report any time.</p>
        </ConfirmDialog>
      )}
    </div>
  );
}

// -- After approval: what was sent, and how it went --------------------------------

const DEFER_TEXT: Record<string, string> = {
  CROSSED_DAY_BOUNDARY: "it was approved on a different day",
  SNAPSHOT_STALE: "it waited longer than the grace window",
  DATA_DRIFTED: "your tasks changed after you approved it",
  WHATSAPP_DISCONNECTED: "WhatsApp was disconnected until it was too late",
};

function Sent({
  shares,
  groups,
  reviewId,
  config,
}: {
  shares: Share[];
  groups: Group[];
  reviewId: string;
  config: UiConfig;
}) {
  const latest = shares[0]!;
  return (
    <div className="split">
      <section className="card split-main">
        <div className="card-head">
          <h2>The message</h2>
        </div>
        <Report
          text={latest.rendered_body}
          imageUrl={latest.has_image ? `/shares/snapshots/${latest.snapshot_id}/image.png` : null}
        />
      </section>
      <div className="split-side stack">
        {shares.map((share) => (
          <Delivery key={share.job.id} share={share} groups={groups} reviewId={reviewId} config={config} />
        ))}
      </div>
    </div>
  );
}

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
    <section className="panel">
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
            Not sent because {DEFER_TEXT[job.deferred_reason ?? ""] ?? job.deferred_reason}. Start a new report to share
            current data.
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
              <button className="btn btn-sm" disabled={retry.isPending} onClick={() => retry.mutate(r.id)}>
                Retry
              </button>
            )}
          </li>
        ))}
      </ul>
      {retry.error && <ErrorNote error={retry.error} />}
    </section>
  );
}
