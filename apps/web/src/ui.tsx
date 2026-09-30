// Small shared pieces.
import type { ReactNode } from "react";

import { ApiError } from "./api";

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

const TONES: Record<string, string> = {
  SENT: "go",
  CLOSED_NO_SHARE: "go",
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

export function StateChip({ state }: { state: string }) {
  const label = LABELS[state] ?? state.charAt(0) + state.slice(1).toLowerCase().replaceAll("_", " ");
  return <span className={`chip chip-${TONES[state] ?? "neutral"}`}>{label}</span>;
}
