// Pure helpers -- everything here is unit-tested in test/logic.test.ts.
// Times are always shown in the configured timezone (Settings.timezone), not
// whatever the browser happens to think, so the UI agrees with the report.

import type { ApprovalRequest, Group, Share } from "./api";
import type { Route } from "./router";

/** "YYYY-MM-DD" for `at` as seen in `tz`. */
export function localDateIn(tz: string, at: Date = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(at);
}

/** Minutes `tz` is ahead of UTC at instant `at`. */
function offsetMinutes(tz: string, at: Date): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    hourCycle: "h23",
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    minute: "numeric",
    second: "numeric",
  }).formatToParts(at);
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value ?? 0);
  const asUtc = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second"));
  return Math.round((asUtc - Math.floor(at.getTime() / 1000) * 1000) / 60000);
}

/** The instant at which the wall clock in `tz` reads `date` `time`
 * ("2026-09-15", "17:30"). */
export function zonedWallTimeToUtc(date: string, time: string, tz: string): Date {
  const [y, mo, d] = date.split("-").map(Number);
  const [h, mi] = time.split(":").map(Number);
  const naive = Date.UTC(y ?? 0, (mo ?? 1) - 1, d ?? 1, h ?? 0, mi ?? 0);
  // Two passes settle the offset even across a DST change.
  let guess = naive - offsetMinutes(tz, new Date(naive)) * 60000;
  guess = naive - offsetMinutes(tz, new Date(guess)) * 60000;
  return new Date(guess);
}

export function formatTime(iso: string | null | undefined, tz: string): string {
  if (!iso) return "—";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: tz,
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(iso));
}

export function formatDate(isoDate: string): string {
  // A bare calendar date: format it as UTC so no timezone can shift the day.
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC",
    weekday: "short",
    day: "numeric",
    month: "short",
  }).format(new Date(`${isoDate}T00:00:00Z`));
}

export const KIND_LABELS: Record<ApprovalRequest["kind"], string> = {
  MORNING: "Morning report",
  EVENING: "End of day report",
  MANUAL: "Manual report",
};

const COMMITTABLE = new Set<ApprovalRequest["state"]>(["REVIEW_PENDING", "USER_EDITING", "READY"]);

/** Whether this review can still be shared or closed. */
export function isOpen(state: ApprovalRequest["state"]): boolean {
  return COMMITTABLE.has(state);
}

/** Groups ticked by default: enabled groups flagged for this kind of report.
 * A manual report borrows the evening defaults -- it's a general status
 * snapshot, the same reasoning the backend uses for its template. */
export function defaultRecipientIds(kind: ApprovalRequest["kind"], groups: Group[]): string[] {
  return groups
    .filter((g) => g.enabled && (kind === "MORNING" ? g.default_morning : g.default_evening))
    .map((g) => g.id);
}

export function shareButtonLabel(count: number): string {
  if (count === 0) return "Pick a group to share";
  return `Share to ${count} ${count === 1 ? "group" : "groups"}`;
}

/** Job states that will still change on their own -- worth polling. */
export function isSettling(share: Share): boolean {
  return ["PENDING", "SCHEDULED", "RESCHEDULED", "SENDING"].includes(share.job.state);
}

/** "at" with no `date` means later today; with one, that calendar day (in
 * the configured timezone). */
export type SendChoice =
  | { kind: "now" }
  | { kind: "in5" }
  | { kind: "at"; time: string; date?: string };

/** How far ahead a report may be scheduled. */
export const MAX_SCHEDULE_DAYS = 7;
export const SNOOZE_MINUTES = 30;

/** How to schedule a commit, or an error sentence. `{sendAt: null,
 * delayMinutes: null}` means now. "In 5 minutes" is a *delay* the server
 * counts from its own clock, not a timestamp from this browser's. */
export function resolveSendAt(
  choice: SendChoice,
  tz: string,
  now: Date = new Date(),
): { sendAt: string | null; delayMinutes: number | null } | { error: string } {
  if (choice.kind === "now") return { sendAt: null, delayMinutes: null };
  if (choice.kind === "in5") return { sendAt: null, delayMinutes: 5 };
  if (!/^\d{2}:\d{2}$/.test(choice.time)) return { error: "Pick a time to send at." };
  const today = localDateIn(tz, now);
  const date = choice.date ?? today;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return { error: "Pick a date to send on." };
  const at = zonedWallTimeToUtc(date, choice.time, tz);
  if (at.getTime() <= now.getTime()) {
    return { error: date === today ? "That time has already passed today." : "That time has already passed." };
  }
  if (at.getTime() > now.getTime() + MAX_SCHEDULE_DAYS * 86_400_000) {
    return { error: `Pick a time within the next ${MAX_SCHEDULE_DAYS} days.` };
  }
  return { sendAt: at.toISOString(), delayMinutes: null };
}

/** Whether a scheduled send lands on a later calendar day than today -- the
 * report is frozen when approved, so it will carry today's data. */
export function isLaterDay(choice: SendChoice, tz: string, now: Date = new Date()): boolean {
  return choice.kind === "at" && !!choice.date && choice.date > localDateIn(tz, now);
}

/** Whether the popup should be showing this review right now: not on that
 * review's own page, where the page itself is the decision. */
export function showPromptOn(route: Route, review: { id: string } | null | undefined): boolean {
  if (!review) return false;
  return !(route.name === "review" && route.id === review.id);
}

/** A key that changes when the popup genuinely comes back (a snooze ended),
 * so a desktop notification fires once per appearance, not once per poll. */
export function promptAppearanceKey(review: { id: string; snoozed_until?: string | null }): string {
  return `${review.id}:${review.snoozed_until ?? ""}`;
}

export interface Segment {
  text: string;
  bold: boolean;
  italic: boolean;
}

/** Split one line of WhatsApp text into styled runs: `*bold*`, `_italic_`.
 * Rendered as React text nodes, so nothing here is ever treated as HTML. */
export function whatsappSegments(line: string): Segment[] {
  const segments: Segment[] = [];
  const pattern = /\*([^*\n]+)\*|_([^_\n]+)_/g;
  let last = 0;
  for (const match of line.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > last) segments.push({ text: line.slice(last, index), bold: false, italic: false });
    if (match[1] !== undefined) segments.push({ text: match[1], bold: true, italic: false });
    else segments.push({ text: match[2] ?? "", bold: false, italic: true });
    last = index + match[0].length;
  }
  if (last < line.length) segments.push({ text: line.slice(last), bold: false, italic: false });
  return segments;
}

// -- WhatsApp connection -----------------------------------------------------

export interface WhatsAppBannerInfo {
  tone: "stop" | "warn";
  title: string;
  message: string;
  /** What the button does: start a QR link, reconnect a still-valid link, or nothing. */
  action: "link" | "reconnect" | null;
  actionLabel: string;
}

const WAITS = "Reports you approve meanwhile wait, and go out after you reconnect if it is still the same day.";

/** What the page-wide banner should say about WhatsApp, or null when all is
 * well. Pure: the wording lives here so every case is tested. A brief
 * "reconnecting" (no reason) never gets a banner -- the status pill covers it. */
export function whatsappBanner(
  status: { provider: string; state: string; reason?: string | null } | undefined,
): WhatsAppBannerInfo | null {
  if (!status || status.provider === "mock" || status.state === "CONNECTED") return null;
  switch (status.reason) {
    case "AGENT_OFFLINE":
      return {
        tone: "warn",
        title: "The WhatsApp service isn't running",
        message: `Interlock's WhatsApp service has stopped. Start Interlock again. ${WAITS}`,
        action: null,
        actionLabel: "",
      };
    case "NOT_LINKED":
      return {
        tone: "stop",
        title: "WhatsApp isn't linked yet",
        message: "Link a phone to start sending reports to WhatsApp.",
        action: "link",
        actionLabel: "Link WhatsApp",
      };
    case "LOGGED_OUT":
      return {
        tone: "stop",
        title: "WhatsApp disconnected",
        message: `It was logged out, for example from Linked Devices on the phone. ${WAITS}`,
        action: "link",
        actionLabel: "Reconnect WhatsApp",
      };
    case "SESSION_INVALID":
      return {
        tone: "stop",
        title: "WhatsApp disconnected",
        message: `The saved connection is no longer valid. ${WAITS}`,
        action: "link",
        actionLabel: "Reconnect WhatsApp",
      };
    case "REPLACED":
      return {
        tone: "warn",
        title: "WhatsApp is connected somewhere else",
        message: `Another session took over this link. ${WAITS}`,
        action: "reconnect",
        actionLabel: "Reconnect here",
      };
    case "FORBIDDEN":
      return {
        tone: "stop",
        title: "WhatsApp refused this account",
        message: "Check WhatsApp on the phone, or link a different phone.",
        action: "link",
        actionLabel: "Link a different phone",
      };
  }
  if (status.state === "LOGIN_REQUIRED") {
    return {
      tone: "stop",
      title: "WhatsApp disconnected",
      message: `It needs to be linked again. ${WAITS}`,
      action: "link",
      actionLabel: "Reconnect WhatsApp",
    };
  }
  if (status.state === "UNAVAILABLE" || status.state === "AUTOMATION_ERROR") {
    return {
      tone: "warn",
      title: "WhatsApp is unavailable",
      message: `Interlock can't reach WhatsApp right now. ${WAITS}`,
      action: null,
      actionLabel: "",
    };
  }
  return null; // CONNECTING with no problem: reconnecting by itself
}

/** What the QR dialog shows for each stage of a linking attempt. */
export type LinkView =
  | "preparing"
  | "scan"
  | "scanned"
  | "success"
  | "expired"
  | "failed"
  | "offline";

export function linkView(
  link: { state: string; agent_online: boolean } | undefined,
  startedAgoMs: number,
): LinkView {
  if (link && !link.agent_online) return "offline";
  switch (link?.state) {
    case "WAITING_FOR_SCAN":
      return "scan";
    case "SCANNED":
      return "scanned";
    case "SUCCEEDED":
      return "success";
    case "EXPIRED":
      return "expired";
    case "FAILED":
    case "CANCELLED":
      return "failed";
    case "IDLE":
      // We asked it to start; if it still says nothing is happening, it didn't.
      return startedAgoMs > 10_000 ? "failed" : "preparing";
    default:
      return "preparing";
  }
}
