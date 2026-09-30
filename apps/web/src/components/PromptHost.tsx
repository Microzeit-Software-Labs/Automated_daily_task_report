import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, type ApprovalRequest, type UiConfig } from "../api";
import {
  formatDate,
  formatTime,
  isLaterDay,
  KIND_LABELS,
  localDateIn,
  MAX_SCHEDULE_DAYS,
  promptAppearanceKey,
  resolveSendAt,
  type SendChoice,
  showPromptOn,
  SNOOZE_MINUTES,
} from "../logic";
import { href, navigate, useRoute } from "../router";
import { useShare } from "../useShare";
import { ErrorNote } from "../ui";
import { BellIcon, Modal } from "./Modal";
import { Bubble } from "./ReportPreview";

/** The 09:00 / 17:00 popup.
 *
 * Stateless on purpose: it only asks the server "is a report waiting on me?"
 * (GET /notifications/prompt). The answer comes from the open review in
 * Postgres, so a restart, a refresh or a second tab can neither lose the
 * popup nor show it twice -- and a snooze is a timestamp on the server, not a
 * timer in this page.
 */
export function PromptHost({ config }: { config: UiConfig }) {
  const route = useRoute();
  const prompt = useQuery({
    queryKey: ["prompt"],
    queryFn: api.prompt,
    refetchInterval: 20_000,
    refetchOnWindowFocus: true,
    staleTime: 0,
  });
  const review = prompt.data?.prompt ?? null;
  const visible = showPromptOn(route, review);
  const [done, setDone] = useState<{ message: string; reviewId: string } | null>(null);

  useDesktopAlert(visible ? review : null);
  useAttentionTitle(visible);

  useEffect(() => {
    if (!done) return;
    const timer = setTimeout(() => setDone(null), 5_000);
    return () => clearTimeout(timer);
  }, [done]);

  if (done) {
    return (
      <Modal title="Done" icon={<BellIcon />} onClose={() => setDone(null)}>
        <p data-autofocus tabIndex={-1} className="prompt-done">
          ✓ {done.message}
        </p>
        <div className="actions">
          <a
            className="btn btn-quiet"
            href={href({ name: "review", id: done.reviewId })}
            onClick={() => setDone(null)}
          >
            See delivery
          </a>
          <button className="btn" onClick={() => setDone(null)}>
            OK
          </button>
        </div>
      </Modal>
    );
  }
  if (!review || !visible) return null;
  return <PromptDialog key={review.id} review={review} config={config} onDone={setDone} />;
}

function PromptDialog({
  review,
  config,
  onDone,
}: {
  review: ApprovalRequest;
  config: UiConfig;
  onDone: (done: { message: string; reviewId: string }) => void;
}) {
  const tz = config.timezone;
  const queryClient = useQueryClient();
  const s = useShare({ review, config });
  const snooze = useMutation({
    mutationFn: (minutes: number) => api.snooze(review.id, minutes),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["prompt"] }),
  });

  const [customOpen, setCustomOpen] = useState(false);
  const today = localDateIn(tz);
  const [date, setDate] = useState(today);
  const [time, setTime] = useState("");
  const custom: SendChoice = { kind: "at", date, time };
  const customResolved = resolveSendAt(custom, tz);
  const maxDate = localDateIn(tz, new Date(Date.now() + MAX_SCHEDULE_DAYS * 86_400_000));

  const names = s.chosen.map((g) => g.display_name).join(", ");
  const send = (choice: SendChoice) => {
    const message =
      choice.kind === "now"
        ? `Sending to ${names} now.`
        : choice.kind === "in5"
          ? `Scheduled to ${names} in 5 minutes.`
          : `Scheduled to ${names} for ${formatDate(choice.date ?? today)} at ${choice.time}.`;
    s.share.mutate(choice, { onSuccess: () => onDone({ message, reviewId: review.id }) });
  };

  const busy = s.busy || snooze.isPending;
  const error = snooze.error ?? s.skip.error ?? (s.drifted ? null : s.share.error);
  const preview = s.preview.data;

  return (
    <Modal
      title="Task report ready"
      icon={<BellIcon />}
      onClose={() => snooze.mutate(SNOOZE_MINUTES)}
      closeLabel={`Close — remind me in ${SNOOZE_MINUTES} minutes`}
    >
      <p className="muted">
        {KIND_LABELS[review.kind]} · opened {formatTime(review.scheduled_for, tz)}
      </p>
      <p className="prompt-question">Would you like to send today's report to a WhatsApp group?</p>

      {(s.changedNotice || s.drifted) && (
        <div className="note note-warn" role="status">
          <strong>Your tasks changed</strong>
          <span>The report below has been updated. Check it before you send.</span>
          <button className="link" onClick={s.dismissChanged}>
            Got it
          </button>
        </div>
      )}

      <div className="prompt-preview">
        {s.preview.error ? (
          <ErrorNote error={s.preview.error} />
        ) : !preview ? (
          <p className="muted">Preparing the report…</p>
        ) : (
          <a href={href({ name: "review", id: review.id })} title="Open the full report">
            {preview.has_image && (
              <img
                className="prompt-image"
                alt="Report table that will be sent"
                src={`/approval-requests/${review.id}/preview.png?h=${preview.content_hash}`}
              />
            )}
            <span className="prompt-caption">
              <Bubble text={preview.rendered_body} bare />
            </span>
          </a>
        )}
      </div>

      <fieldset className="prompt-groups">
        <legend>Send to</legend>
        {s.groups.error ? (
          <ErrorNote error={s.groups.error} />
        ) : s.enabled.length === 0 ? (
          <p className="muted small">
            No WhatsApp groups yet. <a href={href({ name: "groups" })}>Add a group</a> first.
          </p>
        ) : (
          s.enabled.map((g) => (
            <label key={g.id} className="check">
              <input
                type="checkbox"
                checked={s.isSelected(g.id)}
                onChange={(e) => s.toggle(g.id, e.target.checked)}
              />
              <span>{g.display_name}</span>
            </label>
          ))
        )}
      </fieldset>

      {s.status.data && !s.status.data.can_send && (
        <div className="note note-warn" role="status">
          <strong>WhatsApp isn't connected</strong>
          <span>
            You can still send: the report waits and goes out once WhatsApp reconnects, as long as it is
            still today.
          </span>
        </div>
      )}
      {error && <ErrorNote error={error} />}

      <div className="prompt-actions">
        <button
          className="btn btn-primary"
          data-autofocus
          disabled={!s.ready || busy}
          onClick={() => send({ kind: "now" })}
        >
          {s.share.isPending ? "Sending…" : "Send now"}
        </button>
        <button className="btn" disabled={!s.ready || busy} onClick={() => send({ kind: "in5" })}>
          Send after 5 minutes
        </button>
        {config.allow_custom_send_time && (
          <button
            className="btn"
            aria-expanded={customOpen}
            disabled={busy}
            onClick={() => setCustomOpen((open) => !open)}
          >
            Custom time…
          </button>
        )}
        <button className="btn btn-quiet" disabled={busy} onClick={() => snooze.mutate(SNOOZE_MINUTES)}>
          Snooze {SNOOZE_MINUTES} minutes
        </button>
      </div>

      {customOpen && (
        <div className="prompt-custom">
          <label>
            Date
            <input
              type="date"
              className="input"
              value={date}
              min={today}
              max={maxDate}
              onChange={(e) => setDate(e.target.value)}
            />
          </label>
          <label>
            Time
            <input type="time" className="input" value={time} onChange={(e) => setTime(e.target.value)} />
          </label>
          <button
            className="btn btn-primary"
            disabled={!s.ready || busy || "error" in customResolved}
            onClick={() => send(custom)}
          >
            Schedule
          </button>
          {"error" in customResolved && time !== "" && (
            <p className="field-error">{customResolved.error}</p>
          )}
          {isLaterDay(custom, tz) && (
            <p className="muted small">
              The report is frozen when you schedule it, so it will go out with today's data, not that day's.
            </p>
          )}
        </div>
      )}

      <p className="prompt-links small">
        <a href={href({ name: "review", id: review.id })}>Open full report</a>
        {" · "}
        <button
          className="link"
          disabled={busy}
          onClick={() => {
            if (window.confirm("Close this report without sharing it?")) s.skip.mutate();
          }}
        >
          Skip this report
        </button>
      </p>
    </Modal>
  );
}

/** A desktop notification when the popup appears while this tab is in the
 * background. Once per appearance: a snooze ending counts as a new one, a
 * poll that sees the same prompt again does not. */
function useDesktopAlert(review: ApprovalRequest | null) {
  const notified = useRef(new Set<string>());
  useEffect(() => {
    if (!review) return;
    const key = promptAppearanceKey(review);
    if (notified.current.has(key)) return;
    notified.current.add(key);
    if (document.visibilityState === "visible") return; // the popup itself is the alert
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    const n = new Notification(`${KIND_LABELS[review.kind]} is ready`, {
      body: "Send it to WhatsApp, schedule it, or snooze.",
      tag: review.id,
    });
    n.onclick = () => {
      window.focus();
      navigate({ name: "dashboard" });
      n.close();
    };
  }, [review]);
}

/** Puts a marker in the tab title while a report is waiting. */
function useAttentionTitle(active: boolean) {
  useEffect(() => {
    if (!active) return;
    const original = document.title;
    document.title = "● Report ready — Interlock";
    return () => {
      document.title = original;
    };
  }, [active]);
}
