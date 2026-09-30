// Pure helpers -- everything here is unit-tested in test/logic.test.ts.
// Times are always shown in the configured timezone (Settings.timezone), not
// whatever the browser happens to think, so the UI agrees with the report.

import type { ApprovalRequest, Group, Share } from "./api";

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

export type SendChoice = { kind: "now" } | { kind: "in5" } | { kind: "at"; time: string };

/** The `send_at` to commit with, or an error sentence. `null` means now. */
export function resolveSendAt(
  choice: SendChoice,
  tz: string,
  now: Date = new Date(),
): { sendAt: string | null } | { error: string } {
  if (choice.kind === "now") return { sendAt: null };
  if (choice.kind === "in5") return { sendAt: new Date(now.getTime() + 5 * 60000).toISOString() };
  if (!/^\d{2}:\d{2}$/.test(choice.time)) return { error: "Pick a time to send at." };
  const at = zonedWallTimeToUtc(localDateIn(tz, now), choice.time, tz);
  if (at.getTime() <= now.getTime()) return { error: "That time has already passed today." };
  return { sendAt: at.toISOString() };
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
