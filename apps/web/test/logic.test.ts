import { describe, expect, it } from "vitest";

import {
  type ApprovalRequest,
  type DeliverySummary,
  errorFromBody,
  type Group,
  type Share,
} from "../src/api";
import {
  defaultRecipientIds,
  deliveryResult,
  formatDate,
  formatTime,
  groupsLabel,
  isLaterDay,
  isOpen,
  isSettling,
  linkView,
  localDateIn,
  matchesFilter,
  nextReport,
  periodStart,
  promptAppearanceKey,
  reportBanner,
  resolveSendAt,
  shareButtonLabel,
  shiftDate,
  showPromptOn,
  untilLabel,
  whatsappBanner,
  whatsappIsDown,
  whatsappSegments,
  workingDaysLabel,
  zonedWallTimeToUtc,
} from "../src/logic";

const IST = "Asia/Kolkata";

describe("timezones", () => {
  it("reads the local date in the configured zone, not UTC", () => {
    // 20:00 UTC on the 15th is 01:30 on the 16th in IST.
    expect(localDateIn(IST, new Date("2026-09-15T20:00:00Z"))).toBe("2026-09-16");
  });

  it("converts an IST wall time to the right instant", () => {
    expect(zonedWallTimeToUtc("2026-09-15", "17:30", IST).toISOString()).toBe(
      "2026-09-15T12:00:00.000Z",
    );
  });

  it("handles a zone with DST", () => {
    // New York is UTC-4 in September.
    expect(zonedWallTimeToUtc("2026-09-15", "09:00", "America/New_York").toISOString()).toBe(
      "2026-09-15T13:00:00.000Z",
    );
  });

  it("formats times in the configured zone", () => {
    expect(formatTime("2026-09-15T11:30:00Z", IST)).toBe("17:00");
    expect(formatTime(null, IST)).toBe("—");
  });
});

describe("resolveSendAt", () => {
  const now = new Date("2026-09-15T11:30:00Z"); // 17:00 IST

  it("now means no send_at", () => {
    expect(resolveSendAt({ kind: "now" }, IST, now)).toEqual({ sendAt: null, delayMinutes: null });
  });

  it("in five minutes is a delay for the server to count, not a browser timestamp", () => {
    expect(resolveSendAt({ kind: "in5" }, IST, now)).toEqual({ sendAt: null, delayMinutes: 5 });
  });

  it("a later time today", () => {
    expect(resolveSendAt({ kind: "at", time: "21:30" }, IST, now)).toEqual({
      sendAt: "2026-09-15T16:00:00.000Z",
      delayMinutes: null,
    });
  });

  it("refuses a time already passed, and a blank one", () => {
    expect(resolveSendAt({ kind: "at", time: "16:59" }, IST, now)).toHaveProperty("error");
    expect(resolveSendAt({ kind: "at", time: "" }, IST, now)).toHaveProperty("error");
  });
});

describe("resolveSendAt with a date (the popup's custom time)", () => {
  const now = new Date("2026-09-15T11:30:00Z"); // 17:00 IST on the 15th

  it("schedules for a later day", () => {
    expect(resolveSendAt({ kind: "at", date: "2026-09-16", time: "09:00" }, IST, now)).toEqual({
      sendAt: "2026-09-16T03:30:00.000Z",
      delayMinutes: null,
    });
  });

  it("a date of today behaves like later-today", () => {
    expect(resolveSendAt({ kind: "at", date: "2026-09-15", time: "21:30" }, IST, now)).toEqual({
      sendAt: "2026-09-15T16:00:00.000Z",
      delayMinutes: null,
    });
  });

  it("refuses the past, on any day", () => {
    expect(resolveSendAt({ kind: "at", date: "2026-09-15", time: "09:00" }, IST, now)).toEqual({
      error: "That time has already passed today.",
    });
    expect(resolveSendAt({ kind: "at", date: "2026-09-14", time: "21:00" }, IST, now)).toEqual({
      error: "That time has already passed.",
    });
  });

  it("refuses more than a week ahead", () => {
    expect(resolveSendAt({ kind: "at", date: "2026-09-23", time: "09:00" }, IST, now)).toHaveProperty(
      "error",
    );
    expect(resolveSendAt({ kind: "at", date: "2026-09-22", time: "09:00" }, IST, now)).toHaveProperty(
      "sendAt",
    );
  });

  it("refuses a missing or malformed date", () => {
    expect(resolveSendAt({ kind: "at", date: "", time: "09:00" }, IST, now)).toEqual({
      error: "Pick a date to send on.",
    });
  });

  it("knows when a schedule lands on a later day", () => {
    expect(isLaterDay({ kind: "at", date: "2026-09-16", time: "09:00" }, IST, now)).toBe(true);
    expect(isLaterDay({ kind: "at", date: "2026-09-15", time: "21:00" }, IST, now)).toBe(false);
    expect(isLaterDay({ kind: "at", time: "21:00" }, IST, now)).toBe(false);
    expect(isLaterDay({ kind: "in5" }, IST, now)).toBe(false);
  });
});

describe("popup visibility", () => {
  const review = { id: "r1" };

  it("shows for a waiting review anywhere but that review's own page", () => {
    expect(showPromptOn({ name: "dashboard" }, review)).toBe(true);
    expect(showPromptOn({ name: "groups" }, review)).toBe(true);
    expect(showPromptOn({ name: "review", id: "other" }, review)).toBe(true);
    expect(showPromptOn({ name: "review", id: "r1" }, review)).toBe(false);
  });

  it("never shows without a review", () => {
    expect(showPromptOn({ name: "dashboard" }, null)).toBe(false);
    expect(showPromptOn({ name: "dashboard" }, undefined)).toBe(false);
  });

  it("a returning snooze is a new appearance; a repeated poll is not", () => {
    expect(promptAppearanceKey({ id: "r1" })).toBe(promptAppearanceKey({ id: "r1", snoozed_until: null }));
    expect(promptAppearanceKey({ id: "r1", snoozed_until: "2026-09-15T04:00:00Z" })).not.toBe(
      promptAppearanceKey({ id: "r1" }),
    );
  });
});

function group(overrides: Partial<Group>): Group {
  return {
    id: "g",
    display_name: "G",
    external_jid: "g@g.us",
    enabled: true,
    description: "",
    default_morning: false,
    default_evening: false,
    last_used_at: null,
    ...overrides,
  };
}

describe("defaultRecipientIds", () => {
  const groups = [
    group({ id: "am", default_morning: true }),
    group({ id: "pm", default_evening: true }),
    group({ id: "off", default_morning: true, default_evening: true, enabled: false }),
  ];

  it("picks enabled groups flagged for the kind", () => {
    expect(defaultRecipientIds("MORNING", groups)).toEqual(["am"]);
    expect(defaultRecipientIds("EVENING", groups)).toEqual(["pm"]);
  });

  it("manual reports use the evening defaults", () => {
    expect(defaultRecipientIds("MANUAL", groups)).toEqual(["pm"]);
  });
});

describe("labels and states", () => {
  it("the share button names the count", () => {
    expect(shareButtonLabel(0)).toBe("Pick a group to share");
    expect(shareButtonLabel(1)).toBe("Share to 1 group");
    expect(shareButtonLabel(3)).toBe("Share to 3 groups");
  });

  it("only pending/editing/ready reviews are open", () => {
    expect(isOpen("REVIEW_PENDING")).toBe(true);
    expect(isOpen("USER_EDITING")).toBe(true);
    expect(isOpen("APPROVED")).toBe(false);
    expect(isOpen("CLOSED_NO_SHARE")).toBe(false);
  });

  it("polls only while a job can still change on its own", () => {
    const share = (state: Share["job"]["state"]) => ({ job: { state } }) as Share;
    expect(isSettling(share("SCHEDULED"))).toBe(true);
    expect(isSettling(share("SENDING"))).toBe(true);
    expect(isSettling(share("SENT"))).toBe(false);
    expect(isSettling(share("PARTIALLY_SENT"))).toBe(false);
  });
});

describe("whatsappSegments", () => {
  it("styles bold and italic runs", () => {
    expect(whatsappSegments("*Title* and _note_!")).toEqual([
      { text: "Title", bold: true, italic: false },
      { text: " and ", bold: false, italic: false },
      { text: "note", bold: false, italic: true },
      { text: "!", bold: false, italic: false },
    ]);
  });

  it("leaves unmatched markers and markup alone", () => {
    expect(whatsappSegments("2 * 3 <b>x</b>")).toEqual([
      { text: "2 * 3 <b>x</b>", bold: false, italic: false },
    ]);
  });
});

describe("errorFromBody", () => {
  it("reads the domain envelope", () => {
    const err = errorFromBody(409, {
      error: { code: "DATASET_VERSION_CONFLICT", message: "Your tasks changed." },
    });
    expect(err.code).toBe("DATASET_VERSION_CONFLICT");
    expect(err.message).toBe("Your tasks changed.");
  });

  it("reads FastAPI validation errors", () => {
    const err = errorFromBody(422, { detail: [{ msg: "field required" }] });
    expect(err.message).toBe("field required");
  });

  it("falls back to the status", () => {
    expect(errorFromBody(500, "oops").message).toContain("500");
  });
});

describe("whatsappBanner", () => {
  const status = (state: string, reason?: string, provider = "local_agent") => ({ provider, state, reason });

  it("says nothing while connected, in test mode, or before the first answer", () => {
    expect(whatsappBanner(undefined)).toBeNull();
    expect(whatsappBanner(status("CONNECTED"))).toBeNull();
    expect(whatsappBanner(status("LOGIN_REQUIRED", "LOGGED_OUT", "mock"))).toBeNull();
  });

  it("says nothing while it is merely reconnecting by itself", () => {
    expect(whatsappBanner(status("CONNECTING"))).toBeNull();
  });

  it("a logged-out link offers a new QR, and says reports wait", () => {
    const banner = whatsappBanner(status("LOGIN_REQUIRED", "LOGGED_OUT"));
    expect(banner).toMatchObject({ tone: "stop", action: "link", actionLabel: "Reconnect WhatsApp" });
    expect(banner?.title).toBe("WhatsApp disconnected");
    expect(banner?.message).toContain("wait");
  });

  it("an invalid session offers a new QR", () => {
    expect(whatsappBanner(status("LOGIN_REQUIRED", "SESSION_INVALID"))).toMatchObject({
      tone: "stop",
      action: "link",
    });
  });

  it("a link that was never made offers to link", () => {
    expect(whatsappBanner(status("LOGIN_REQUIRED", "NOT_LINKED"))).toMatchObject({
      title: "WhatsApp isn't linked yet",
      action: "link",
      actionLabel: "Link WhatsApp",
    });
  });

  it("a taken-over link reconnects in place instead of asking for a QR", () => {
    expect(whatsappBanner(status("UNAVAILABLE", "REPLACED"))).toMatchObject({
      tone: "warn",
      action: "reconnect",
      actionLabel: "Reconnect here",
    });
  });

  it("a refused account points at the phone, and offers a different one", () => {
    expect(whatsappBanner(status("UNAVAILABLE", "FORBIDDEN"))).toMatchObject({
      tone: "stop",
      action: "link",
      actionLabel: "Link a different phone",
    });
  });

  it("a stopped service is a different problem: no QR button, just how to recover", () => {
    const banner = whatsappBanner(status("UNAVAILABLE", "AGENT_OFFLINE"));
    expect(banner).toMatchObject({ tone: "warn", action: null });
    expect(banner?.title).toBe("The WhatsApp service isn't running");
  });

  it("falls back sensibly for states that carry no reason", () => {
    expect(whatsappBanner(status("LOGIN_REQUIRED"))).toMatchObject({ action: "link" });
    expect(whatsappBanner(status("UNAVAILABLE"))).toMatchObject({ tone: "warn", action: null });
    expect(whatsappBanner(status("AUTOMATION_ERROR"))).toMatchObject({ tone: "warn", action: null });
  });
});

describe("linkView", () => {
  const link = (state: string, agent_online = true) => ({ state, agent_online });

  it("maps each stage of a linking attempt", () => {
    expect(linkView(undefined, 0)).toBe("preparing");
    expect(linkView(link("STARTING"), 0)).toBe("preparing");
    expect(linkView(link("WAITING_FOR_SCAN"), 0)).toBe("scan");
    expect(linkView(link("SCANNED"), 0)).toBe("scanned");
    expect(linkView(link("SUCCEEDED"), 0)).toBe("success");
    expect(linkView(link("EXPIRED"), 0)).toBe("expired");
    expect(linkView(link("FAILED"), 0)).toBe("failed");
    expect(linkView(link("CANCELLED"), 0)).toBe("failed");
  });

  it("a service that is not running overrides everything", () => {
    expect(linkView(link("WAITING_FOR_SCAN", false), 0)).toBe("offline");
    expect(linkView(link("IDLE", false), 0)).toBe("offline");
  });

  it("'nothing is happening' right after asking is still preparing, but not for long", () => {
    expect(linkView(link("IDLE"), 3_000)).toBe("preparing");
    expect(linkView(link("IDLE"), 11_000)).toBe("failed");
  });
});

// -- Reports list ------------------------------------------------------------

function delivery(overrides: Partial<DeliverySummary> = {}): DeliverySummary {
  return {
    job_state: "SENT",
    run_at: "2026-09-15T11:30:00Z",
    sent_at: "2026-09-15T11:30:05Z",
    deferred_reason: null,
    total: 3,
    sent: 3,
    failed: 0,
    pending: 0,
    skipped: 0,
    group_names: ["Dev", "Ops", "Test"],
    ...overrides,
  };
}

describe("deliveryResult", () => {
  const now = new Date("2026-09-15T11:30:00Z"); // 17:00 IST
  const ctx = { tz: IST, now, whatsappDown: false };
  const result = (state: string, d?: Partial<DeliverySummary> | null, over = {}) =>
    deliveryResult({ state } as ApprovalRequest, d === null ? null : delivery(d), { ...ctx, ...over });

  it("a report still waiting on you needs approval, whatever else it carries", () => {
    for (const state of ["REVIEW_PENDING", "USER_EDITING", "READY"]) {
      expect(result(state, null)).toEqual({
        label: "Needs your approval",
        tone: "warn",
        category: "needs_approval",
      });
    }
  });

  it("a report that was closed, cancelled or never approved was not shared", () => {
    expect(result("CLOSED_NO_SHARE", null)).toMatchObject({ label: "Not shared", category: "not_shared" });
    expect(result("CANCELLED", null)).toMatchObject({ label: "Cancelled", category: "not_shared" });
    expect(result("EXPIRED", null)).toMatchObject({ label: "Expired", category: "not_shared" });
  });

  it("says how many groups got it", () => {
    expect(result("APPROVED")).toEqual({ label: "Sent to 3 groups", tone: "go", category: "sent" });
    expect(result("APPROVED", { sent: 1, total: 1 })).toMatchObject({ label: "Sent to 1 group" });
  });

  it("a partly sent report is a problem and says how partly", () => {
    expect(result("APPROVED", { job_state: "PARTIALLY_SENT", sent: 2, failed: 1 })).toEqual({
      label: "Partly sent 2/3",
      tone: "warn",
      category: "problem",
    });
  });

  it("failed and held back are problems", () => {
    expect(result("APPROVED", { job_state: "FAILED", sent: 0, failed: 3 })).toMatchObject({
      label: "Failed",
      tone: "stop",
      category: "problem",
    });
    expect(result("APPROVED", { job_state: "DEFERRED" })).toMatchObject({
      label: "Held back",
      category: "problem",
    });
  });

  it("a send in the future says when, on today's clock or with the day", () => {
    const later = { job_state: "SCHEDULED" as const, sent: 0, sent_at: null };
    expect(result("APPROVED", { ...later, run_at: "2026-09-15T16:00:00Z" })).toEqual({
      label: "Scheduled 21:30",
      tone: "info",
      category: "in_progress",
    });
    // On another day the day is spelled out, as the rest of the UI spells it.
    expect(result("APPROVED", { ...later, run_at: "2026-09-16T03:30:00Z" })).toMatchObject({
      label: `Scheduled ${formatDate("2026-09-16")} 09:00`,
    });
  });

  it("a send that is due while WhatsApp is down is waiting, not failed", () => {
    const due = { job_state: "SCHEDULED" as const, sent: 0, run_at: "2026-09-15T11:00:00Z" };
    expect(result("APPROVED", due, { whatsappDown: true })).toEqual({
      label: "Waiting for WhatsApp",
      tone: "warn",
      category: "in_progress",
    });
    expect(result("APPROVED", due, { whatsappDown: false })).toMatchObject({ label: "Sending" });
  });

  it("a send due within the minute already counts as sending, not scheduled", () => {
    // "Now" is stamped by the server a moment after this page last read its clock.
    const soon = { job_state: "SCHEDULED" as const, sent: 0, run_at: "2026-09-15T11:30:40Z" };
    expect(result("APPROVED", soon)).toMatchObject({ label: "Sending" });
    const later = { ...soon, run_at: "2026-09-15T11:32:00Z" };
    expect(result("APPROVED", later)).toMatchObject({ label: "Scheduled 17:02" });
  });

  it("being down does not change a send that is not due yet", () => {
    const later = { job_state: "SCHEDULED" as const, sent: 0, run_at: "2026-09-15T16:00:00Z" };
    expect(result("APPROVED", later, { whatsappDown: true })).toMatchObject({
      label: "Scheduled 21:30",
    });
  });

  it("an approved report with no send recorded is just approved", () => {
    expect(result("APPROVED", null)).toMatchObject({ label: "Approved", category: "in_progress" });
  });
});

describe("report filters and periods", () => {
  it("each filter keeps its own category, and 'All' keeps everything", () => {
    const sent = { label: "", tone: "go", category: "sent" } as const;
    const scheduled = { label: "", tone: "info", category: "in_progress" } as const;
    expect(matchesFilter(sent, "sent")).toBe(true);
    expect(matchesFilter(sent, "problem")).toBe(false);
    expect(matchesFilter(scheduled, "all")).toBe(true);
    expect(matchesFilter(scheduled, "sent")).toBe(false);
  });

  it("a period starts that many days back, today included", () => {
    const now = new Date("2026-09-15T11:30:00Z");
    expect(periodStart(1, IST, now)).toBe("2026-09-15");
    expect(periodStart(7, IST, now)).toBe("2026-09-09");
    expect(periodStart(30, IST, now)).toBe("2026-08-17");
  });

  it("'today' is today in the configured zone, not UTC", () => {
    // 20:00 UTC on the 15th is already the 16th in IST.
    expect(periodStart(1, IST, new Date("2026-09-15T20:00:00Z"))).toBe("2026-09-16");
  });

  it("shifting a date crosses month and year ends", () => {
    expect(shiftDate("2026-03-01", -1)).toBe("2026-02-28");
    expect(shiftDate("2026-12-31", 1)).toBe("2027-01-01");
  });

  it("groups are listed, then counted", () => {
    expect(groupsLabel([])).toBe("—");
    expect(groupsLabel(["Ops"])).toBe("Ops");
    expect(groupsLabel(["Dev", "Ops"])).toBe("Dev, Ops");
    expect(groupsLabel(["A", "B", "C", "D"])).toBe("A, B +2");
  });
});

describe("the next report", () => {
  const config = {
    timezone: IST,
    morning_alert_time: "09:00:00",
    evening_alert_time: "17:00:00",
    working_days: [0, 1, 2, 3, 4],
  };

  it("before 09:00 the next report is this morning's", () => {
    const next = nextReport(config, new Date("2026-09-15T02:00:00Z")); // 07:30 Tue
    expect(next?.kind).toBe("MORNING");
    expect(next?.at.toISOString()).toBe("2026-09-15T03:30:00.000Z");
  });

  it("between the two it is this evening's", () => {
    const next = nextReport(config, new Date("2026-09-15T06:30:00Z")); // 12:00 Tue
    expect(next?.kind).toBe("EVENING");
    expect(next?.at.toISOString()).toBe("2026-09-15T11:30:00.000Z");
  });

  it("after the evening report it is tomorrow's morning", () => {
    const next = nextReport(config, new Date("2026-09-15T13:00:00Z")); // 18:30 Tue
    expect(next?.kind).toBe("MORNING");
    expect(next?.at.toISOString()).toBe("2026-09-16T03:30:00.000Z");
  });

  it("skips the weekend", () => {
    const next = nextReport(config, new Date("2026-09-18T13:00:00Z")); // 18:30 Fri
    expect(next?.at.toISOString()).toBe("2026-09-21T03:30:00.000Z"); // Mon 09:00
  });

  it("is null when no day is a working day", () => {
    expect(nextReport({ ...config, working_days: [] }, new Date("2026-09-15T02:00:00Z"))).toBeNull();
  });

  it("describes how far away it is", () => {
    const now = new Date("2026-09-15T08:00:00Z");
    const at = (minutes: number) => new Date(now.getTime() + minutes * 60_000);
    expect(untilLabel(at(0), IST, now)).toBe("now");
    expect(untilLabel(at(25), IST, now)).toBe("in 25 min");
    expect(untilLabel(at(120), IST, now)).toBe("in 2 h");
    expect(untilLabel(at(130), IST, now)).toBe("in 2 h 10 min");
    expect(untilLabel(at(26 * 60), IST, now)).toBe(`${formatDate("2026-09-16")} 15:30`);
  });

  it("names the working days", () => {
    expect(workingDaysLabel([0, 1, 2, 3, 4])).toBe("Mon to Fri");
    expect(workingDaysLabel([0, 1, 2, 3, 4, 5, 6])).toBe("Every day");
    expect(workingDaysLabel([0, 2, 4])).toBe("Mon, Wed, Fri");
    expect(workingDaysLabel([0, 1])).toBe("Mon, Tue");
    expect(workingDaysLabel([])).toBe("No days");
  });
});

describe("whatsappIsDown", () => {
  it("is down only for a real provider that cannot send", () => {
    expect(whatsappIsDown(undefined)).toBe(false);
    expect(whatsappIsDown({ provider: "local_agent", can_send: false })).toBe(true);
    expect(whatsappIsDown({ provider: "local_agent", can_send: true })).toBe(false);
    expect(whatsappIsDown({ provider: "mock", can_send: false })).toBe(false);
  });
});

describe("reportBanner", () => {
  const ctx = { tz: IST, now: new Date("2026-09-15T11:30:00Z"), whatsappDown: false };
  const share = (state: Share["job"]["state"], recipients: string[]): Share =>
    ({
      job: { state, sent_at: null, deferred_reason: null },
      run_at: "2026-09-15T11:30:00Z",
      recipients: recipients.map((s) => ({ state: s })),
    }) as unknown as Share;

  it("asks for approval while the report is open", () => {
    expect(reportBanner({ state: "REVIEW_PENDING" } as ApprovalRequest, undefined, ctx)).toMatchObject({
      tone: "warn",
      title: "Needs your approval",
    });
  });

  it("reads the delivery from the latest share", () => {
    const banner = reportBanner(
      { state: "APPROVED" } as ApprovalRequest,
      share("PARTIALLY_SENT", ["SENT", "SENT", "FAILED"]),
      ctx,
    );
    expect(banner.title).toBe("Partly sent 2/3");
    expect(banner.message).toMatch(/retry/i);
  });

  it("explains a report that is waiting for WhatsApp", () => {
    const banner = reportBanner({ state: "APPROVED" } as ApprovalRequest, share("SCHEDULED", ["QUEUED"]), {
      ...ctx,
      whatsappDown: true,
    });
    expect(banner).toMatchObject({ tone: "warn", title: "Waiting for WhatsApp" });
  });
});

