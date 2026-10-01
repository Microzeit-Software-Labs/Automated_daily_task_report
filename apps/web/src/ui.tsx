// Small shared pieces: icons, status chips, callouts, error notes.
import type { ReactNode } from "react";

import { ApiError } from "./api";
import type { ReportResult, ResultTone } from "./logic";

export function ErrorNote({ error, children }: { error: unknown; children?: ReactNode }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="note note-stop" role="alert">
      <strong>{error instanceof ApiError && error.code === "NETWORK" ? "Offline" : "Something went wrong"}</strong>
      <span>{message}</span>
      {children}
    </div>
  );
}

// -- Icons ------------------------------------------------------------------------
// Inline, 16px, stroke-only, so they take the colour of the text around them.

export type IconName = "check" | "clock" | "alert" | "x" | "dash" | "send" | "plus" | "arrow" | "info";

const PATHS: Record<IconName, ReactNode> = {
  check: <path d="M4 12.5 9.5 18 20 6.5" />,
  clock: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </>
  ),
  alert: (
    <>
      <path d="M12 4 3 19.5h18L12 4Z" />
      <path d="M12 10v4.5M12 17.2v.1" />
    </>
  ),
  x: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="m9 9 6 6M15 9l-6 6" />
    </>
  ),
  dash: <path d="M6 12h12" />,
  send: <path d="m4 12 16-8-5 16-3.5-6.5L4 12Z" />,
  plus: <path d="M12 5v14M5 12h14" />,
  arrow: <path d="M5 12h14m-5-5 5 5-5 5" />,
  info: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 11v5M12 7.8v.1" />
    </>
  ),
};

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {PATHS[name]}
    </svg>
  );
}

// -- Chips -------------------------------------------------------------------------
// Colour is never the only signal: every chip carries an icon and its words.

const TONE_ICON: Record<ResultTone, IconName> = {
  go: "check",
  warn: "clock",
  stop: "x",
  info: "clock",
  neutral: "dash",
};

export function Chip({ tone, children, icon }: { tone: ResultTone; children: ReactNode; icon?: IconName }) {
  return (
    <span className={`chip chip-${tone}`}>
      <Icon name={icon ?? TONE_ICON[tone]} size={14} />
      {children}
    </span>
  );
}

export function ResultChip({ result }: { result: ReportResult }) {
  return <Chip tone={result.tone}>{result.label}</Chip>;
}

const TONES: Record<string, ResultTone> = {
  SENT: "go",
  CLOSED_NO_SHARE: "neutral",
  APPROVED: "go",
  PARTIALLY_SENT: "warn",
  DEFERRED: "warn",
  SCHEDULED: "info",
  RESCHEDULED: "info",
  SENDING: "info",
  QUEUED: "info",
  PENDING: "info",
  REVIEW_PENDING: "warn",
  USER_EDITING: "warn",
  READY: "warn",
  FAILED: "stop",
  CANCELLED: "neutral",
  EXPIRED: "neutral",
  SKIPPED: "neutral",
  DRAFT: "neutral",
};

const LABELS: Record<string, string> = {
  REVIEW_PENDING: "Waiting for you",
  USER_EDITING: "Waiting for you",
  READY: "Waiting for you",
  APPROVED: "Approved",
  CLOSED_NO_SHARE: "Not shared",
  PARTIALLY_SENT: "Partly sent",
};

/** A chip for a raw state (a recipient's, a job's). Reports in the list use
 * `ResultChip`, which words the outcome instead. */
export function StateChip({ state }: { state: string }) {
  const label = LABELS[state] ?? state.charAt(0) + state.slice(1).toLowerCase().replaceAll("_", " ");
  return <Chip tone={TONES[state] ?? "neutral"}>{label}</Chip>;
}

// -- Callout -----------------------------------------------------------------------

/** A boxed notice: a coloured edge, an icon, a bold title and a sentence. Used
 * for the status at the top of a report. */
export function Callout({
  tone,
  title,
  children,
}: {
  tone: ResultTone;
  title: string;
  children?: ReactNode;
}) {
  const icon: IconName = tone === "go" ? "check" : tone === "stop" ? "alert" : tone === "warn" ? "clock" : "info";
  return (
    <div className={`callout callout-${tone}`} role="status">
      <Icon name={icon} size={20} />
      <div className="callout-text">
        <strong>{title}</strong>
        {children && <span>{children}</span>}
      </div>
    </div>
  );
}
