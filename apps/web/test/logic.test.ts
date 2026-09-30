import { describe, expect, it } from "vitest";

import { errorFromBody, type Group, type Share } from "../src/api";
import {
  defaultRecipientIds,
  formatTime,
  isOpen,
  isSettling,
  localDateIn,
  resolveSendAt,
  shareButtonLabel,
  whatsappSegments,
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
    expect(resolveSendAt({ kind: "now" }, IST, now)).toEqual({ sendAt: null });
  });

  it("in five minutes", () => {
    expect(resolveSendAt({ kind: "in5" }, IST, now)).toEqual({
      sendAt: "2026-09-15T11:35:00.000Z",
    });
  });

  it("a later time today", () => {
    expect(resolveSendAt({ kind: "at", time: "21:30" }, IST, now)).toEqual({
      sendAt: "2026-09-15T16:00:00.000Z",
    });
  });

  it("refuses a time already passed, and a blank one", () => {
    expect(resolveSendAt({ kind: "at", time: "16:59" }, IST, now)).toHaveProperty("error");
    expect(resolveSendAt({ kind: "at", time: "" }, IST, now)).toHaveProperty("error");
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
