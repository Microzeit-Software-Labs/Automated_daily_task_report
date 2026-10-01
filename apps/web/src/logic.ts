// Pure helpers -- everything here is unit-tested in test/logic.test.ts.
// Times are always shown in the configured timezone (Settings.timezone), not
// whatever the browser happens to think, so the UI agrees with the report.

import type {
  ApprovalRequest,
  DeliverySummary,
  Group,
  Share,
  SheetSource,
  UiConfig,
} from "./api";
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

// -- Reports list --------------------------------------------------------------

export type ResultTone = "go" | "warn" | "stop" | "info" | "neutral";

/** Which filter tab a report belongs under. `in_progress` (scheduled, sending,
 * waiting for WhatsApp) shows under "All" only. */
export type ResultCategory = "needs_approval" | "sent" | "problem" | "not_shared" | "in_progress";

export interface ReportResult {
  label: string;
  tone: ResultTone;
  category: ResultCategory;
}

export function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

/** WhatsApp can't send right now (a real provider that isn't connected). The
 * mock always says it can, and is never "down". */
export function whatsappIsDown(
  status: { provider: string; can_send: boolean } | undefined,
): boolean {
  return !!status && status.provider !== "mock" && !status.can_send;
}

/** "17:30" today, or "Wed 16 Sep 17:30" on another day, in `tz`. */
export function whenLabel(
  iso: string | null | undefined,
  tz: string,
  now: Date = new Date(),
): string {
  if (!iso) return "—";
  const day = localDateIn(tz, new Date(iso));
  const time = formatTime(iso, tz);
  return day === localDateIn(tz, now) ? time : `${formatDate(day)} ${time}`;
}

/** What one report's row says in the Reports list: its outcome in words, not
 * the internal state. The words depend on the time and on WhatsApp's
 * connection, which is why the server only sends the numbers. */
export function deliveryResult(
  review: Pick<ApprovalRequest, "state">,
  delivery: DeliverySummary | null | undefined,
  ctx: { tz: string; now: Date; whatsappDown: boolean },
): ReportResult {
  const { state } = review;
  if (isOpen(state)) return { label: "Needs your approval", tone: "warn", category: "needs_approval" };
  if (state === "CLOSED_NO_SHARE") return { label: "Not shared", tone: "neutral", category: "not_shared" };
  if (state === "CANCELLED") return { label: "Cancelled", tone: "neutral", category: "not_shared" };
  if (state === "EXPIRED") return { label: "Expired", tone: "neutral", category: "not_shared" };
  if (!delivery) return { label: "Approved", tone: "info", category: "in_progress" };

  switch (delivery.job_state) {
    case "SENT":
      return { label: `Sent to ${plural(delivery.sent, "group")}`, tone: "go", category: "sent" };
    case "PARTIALLY_SENT":
      return {
        label: `Partly sent ${delivery.sent}/${delivery.total}`,
        tone: "warn",
        category: "problem",
      };
    case "FAILED":
      return { label: "Failed", tone: "stop", category: "problem" };
    case "DEFERRED":
      return { label: "Held back", tone: "warn", category: "problem" };
    case "SENDING":
      return { label: "Sending", tone: "info", category: "in_progress" };
    case "CANCELLED":
    case "SUPERSEDED":
      return { label: "Cancelled", tone: "neutral", category: "not_shared" };
    default: {
      // Waiting its turn: PENDING / SCHEDULED / RESCHEDULED.
      // A minute of slack: "now" is stamped by the server a moment after this
      // page last read its own clock, and the worker picks it up within a tick.
      const due = delivery.run_at ? new Date(delivery.run_at).getTime() <= ctx.now.getTime() + 60_000 : true;
      if (due && ctx.whatsappDown) {
        return { label: "Waiting for WhatsApp", tone: "warn", category: "in_progress" };
      }
      if (due) return { label: "Sending", tone: "info", category: "in_progress" };
      return {
        label: `Scheduled ${whenLabel(delivery.run_at, ctx.tz, ctx.now)}`,
        tone: "info",
        category: "in_progress",
      };
    }
  }
}

/** "Ops, Dev" or "Ops, Dev +2" for the Groups column. */
export function groupsLabel(names: string[] | undefined, shown = 2): string {
  if (!names || names.length === 0) return "—";
  if (names.length <= shown) return names.join(", ");
  return `${names.slice(0, shown).join(", ")} +${names.length - shown}`;
}

export const REPORT_FILTERS = [
  { id: "all", label: "All" },
  { id: "needs_approval", label: "Needs approval" },
  { id: "sent", label: "Sent" },
  { id: "problem", label: "Problems" },
  { id: "not_shared", label: "Not shared" },
] as const;
export type ReportFilter = (typeof REPORT_FILTERS)[number]["id"];

export function matchesFilter(result: ReportResult, filter: ReportFilter): boolean {
  return filter === "all" || result.category === filter;
}

export const REPORT_PERIODS = [
  { id: "today", label: "Today", days: 1 },
  { id: "week", label: "7 days", days: 7 },
  { id: "month", label: "30 days", days: 30 },
] as const;
export type ReportPeriod = (typeof REPORT_PERIODS)[number]["id"];

/** A bare calendar date moved by `days` (no timezone can shift it). */
export function shiftDate(isoDate: string, days: number): string {
  const moved = new Date(`${isoDate}T00:00:00Z`);
  moved.setUTCDate(moved.getUTCDate() + days);
  return moved.toISOString().slice(0, 10);
}

/** The first calendar day a period of `days` days (today included) covers. */
export function periodStart(days: number, tz: string, now: Date = new Date()): string {
  return shiftDate(localDateIn(tz, now), -(days - 1));
}

/** Monday = 0, like the server's `working_days`. */
export function weekdayIndex(isoDate: string): number {
  return (new Date(`${isoDate}T00:00:00Z`).getUTCDay() + 6) % 7;
}

const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** "Mon to Fri", "Every day", or "Mon, Wed, Fri". */
export function workingDaysLabel(days: number[]): string {
  const sorted = [...new Set(days)].filter((d) => d >= 0 && d <= 6).sort((a, b) => a - b);
  if (sorted.length === 0) return "No days";
  if (sorted.length === 7) return "Every day";
  const first = sorted[0] ?? 0;
  const last = sorted[sorted.length - 1] ?? 0;
  if (sorted.length > 2 && last - first === sorted.length - 1) {
    return `${DAY_NAMES[first]} to ${DAY_NAMES[last]}`;
  }
  return sorted.map((d) => DAY_NAMES[d]).join(", ");
}

export interface NextReport {
  kind: "MORNING" | "EVENING";
  at: Date;
}

/** The next 09:00 / 17:00 style report time after `now`, skipping days that
 * aren't working days. Null only if no working day is configured. */
export function nextReport(
  config: Pick<UiConfig, "timezone" | "morning_alert_time" | "evening_alert_time" | "working_days">,
  now: Date = new Date(),
): NextReport | null {
  const today = localDateIn(config.timezone, now);
  const slots: [NextReport["kind"], string][] = [
    ["MORNING", config.morning_alert_time.slice(0, 5)],
    ["EVENING", config.evening_alert_time.slice(0, 5)],
  ];
  for (let offset = 0; offset <= 7; offset++) {
    const date = shiftDate(today, offset);
    if (!config.working_days.includes(weekdayIndex(date))) continue;
    for (const [kind, time] of slots) {
      const at = zonedWallTimeToUtc(date, time, config.timezone);
      if (at.getTime() > now.getTime()) return { kind, at };
    }
  }
  return null;
}

/** "in 25 min", "in 2 h 10 min", or the day and time once it is a day or more away. */
export function untilLabel(at: Date, tz: string, now: Date = new Date()): string {
  const minutes = Math.max(0, Math.round((at.getTime() - now.getTime()) / 60_000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `in ${minutes} min`;
  if (minutes < 24 * 60) {
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return m === 0 ? `in ${h} h` : `in ${h} h ${m} min`;
  }
  return `${formatDate(localDateIn(tz, at))} ${formatTime(at.toISOString(), tz)}`;
}

/** The status banner at the top of a report's own page. */
export interface ReportBanner {
  tone: ResultTone;
  title: string;
  message: string;
}

/** A review's latest send as a summary, from the review detail's `shares`. */
export function summaryFromShare(share: Share): DeliverySummary {
  const states = share.recipients.map((r) => r.state);
  const count = (...wanted: string[]) => states.filter((s) => wanted.includes(s)).length;
  const sent = count("SENT");
  const failed = count("FAILED");
  const skipped = count("SKIPPED", "CANCELLED");
  return {
    job_state: share.job.state,
    run_at: share.run_at ?? null,
    sent_at: share.job.sent_at ?? null,
    deferred_reason: share.job.deferred_reason ?? null,
    total: states.length,
    sent,
    failed,
    pending: states.length - sent - failed - skipped,
    skipped,
    group_names: [],
  };
}

const BANNER_MESSAGES: Record<string, string> = {
  "Needs your approval": "Check the report, choose who gets it and when, then share it.",
  "Not shared": "You closed this report without sharing it.",
  Cancelled: "This report was cancelled and nothing was sent.",
  Expired: "This report was never approved, so nothing was sent.",
  Failed: "Nothing reached WhatsApp. You can retry each group below.",
  "Held back":
    "It was not sent automatically because it was too late. Start a new report to share current data.",
  "Waiting for WhatsApp": "It goes out by itself once WhatsApp reconnects, if it is still the same day.",
  Sending: "It is on its way.",
};

export function reportBanner(
  review: Pick<ApprovalRequest, "state">,
  latest: Share | undefined,
  ctx: { tz: string; now: Date; whatsappDown: boolean },
): ReportBanner {
  const result = deliveryResult(review, latest ? summaryFromShare(latest) : null, ctx);
  let message = BANNER_MESSAGES[result.label] ?? "";
  if (!message && result.label.startsWith("Sent")) message = "Delivered to WhatsApp.";
  if (!message && result.label.startsWith("Partly")) {
    message = "Some groups did not get it. Retry them below.";
  }
  if (!message && result.label.startsWith("Scheduled")) {
    message = "It will be sent at that time. The report was frozen when you approved it.";
  }
  return { tone: result.tone, title: result.label, message };
}

// -- Google Sheet --------------------------------------------------------------

/** A quick look before asking the server: is this plausibly a Google Sheets
 * link? (The server is the judge; this only keeps the Check button honest.) */
export function looksLikeSheetLink(text: string): boolean {
  return /\/spreadsheets\/d\/[A-Za-z0-9_-]+/.test(text.trim());
}

export interface SheetStatus {
  tone: ResultTone;
  title: string;
  /** The reason, when something is wrong. */
  detail?: string;
}

/** The one line (or two) the Settings card shows about the sheet. */
export function sheetStatus(
  sheet: SheetSource | undefined,
  tz: string,
  now: Date = new Date(),
): SheetStatus {
  if (!sheet) return { tone: "neutral", title: "Checking…" };
  if (!sheet.configured) return { tone: "neutral", title: "No sheet is connected." };
  if (sheet.importing) return { tone: "info", title: "Reading the sheet…" };
  if (sheet.last_error) {
    const at = whenLabel(sheet.last_attempt_at, tz, now);
    const earlier = sheet.last_success_at
      ? ` Reports are using the tasks read at ${whenLabel(sheet.last_success_at, tz, now)}.`
      : " No tasks have been read from it yet.";
    return {
      tone: sheet.last_error_code === "UNREACHABLE" ? "warn" : "stop",
      title: `Couldn't read the sheet at ${at}`,
      detail: `${sheet.last_error}${earlier}`,
    };
  }
  if (sheet.last_success_at) {
    const count = sheet.last_task_count ?? 0;
    return {
      tone: "go",
      title: `Last read ${whenLabel(sheet.last_success_at, tz, now)} · ${plural(count, "task")}`,
    };
  }
  return { tone: "neutral", title: "Waiting for the first read…" };
}

export interface SheetBannerInfo {
  tone: "stop" | "warn";
  title: string;
  message: string;
  /** "fix" opens the change-sheet dialog; null means there is nothing to fix. */
  action: "fix" | null;
  actionLabel: string;
}

/** A page-wide notice when the sheet can't be read, or null when all is well.
 * Silent while a new link is still being read, and while the sheet is fine. */
export function sheetBanner(
  sheet: SheetSource | undefined,
  tz: string,
  now: Date = new Date(),
): SheetBannerInfo | null {
  if (!sheet || !sheet.configured || sheet.importing || !sheet.last_error) return null;
  const earlier = sheet.last_success_at
    ? `Reports are using the tasks read at ${whenLabel(sheet.last_success_at, tz, now)}.`
    : "No tasks have been read from it yet.";
  if (sheet.last_error_code === "UNREACHABLE") {
    return {
      tone: "warn",
      title: "Can't reach Google Sheets right now",
      message: `${sheet.last_error} ${earlier}`,
      action: null,
      actionLabel: "",
    };
  }
  return {
    tone: sheet.last_success_at ? "warn" : "stop",
    title: "Your Google Sheet can't be read",
    message: `${sheet.last_error} ${earlier}`,
    action: sheet.changeable ? "fix" : null,
    actionLabel: "Fix the sheet link",
  };
}

/** How far a just-saved sheet has got. `startedAtMs` is when the save returned,
 * `nowMs` the current time; both are plain numbers so this stays pure. */
export type SheetProgress = "importing" | "done" | "failed" | "slow";

export const SHEET_IMPORT_PATIENCE_MS = 90_000;

export function sheetProgress(
  sheet: SheetSource | undefined,
  startedAtMs: number,
  nowMs: number,
): SheetProgress {
  if (sheet && !sheet.importing) {
    // The worker has acted on it: it either read the sheet or wrote down why not.
    if (sheet.last_error) return "failed";
    if (sheet.last_success_at) return "done";
  }
  return nowMs - startedAtMs > SHEET_IMPORT_PATIENCE_MS ? "slow" : "importing";
}
