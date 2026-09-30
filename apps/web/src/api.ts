// Thin typed client over the Interlock API. Every type comes from
// src/api-types.ts, generated from docs/openapi.json (`npm run gen:api`), so
// a backend schema change shows up here as a compile error, not a blank field.

import type { components } from "./api-types";

type S = components["schemas"];
export type ApprovalRequest = S["ApprovalRequestOut"];
// `shares`/`has_image` have server-side defaults, so the generated types mark
// them optional; api.review() and api.preview() always fill them in.
export type ReviewDetail = S["ReviewDetailOut"] & { shares: Share[] };
export type Preview = S["PreviewResponse"] & { has_image: boolean };
export type CommitRequest = S["CommitRequest"];
export type CommitResponse = S["CommitResponse"];
export type Share = S["ShareOut"] & { snapshot_id: string; has_image: boolean };
export type ShareRecipient = S["ShareRecipientOut"];
export type Group = S["WhatsAppGroupOut"];
export type GroupCandidate = S["GroupCandidateOut"];
export type GroupCreate = S["WhatsAppGroupCreateRequest"];
export type WhatsAppStatus = S["WhatsAppStatusOut"];
export type UiConfig = S["UiConfigOut"];

/** A failed request, carrying the API's stable error `code` when there is one. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string | null,
  ) {
    super(message);
  }
}

/** Turn any error body the API can produce into one readable sentence. */
export function errorFromBody(status: number, body: unknown): ApiError {
  if (body && typeof body === "object") {
    const envelope = (body as { error?: { code?: unknown; message?: unknown } }).error;
    if (envelope && typeof envelope.message === "string") {
      return new ApiError(
        envelope.message,
        status,
        typeof envelope.code === "string" ? envelope.code : null,
      );
    }
    // FastAPI's own request-validation shape: {"detail": [{"msg": ...}, ...]}
    const detail = (body as { detail?: unknown }).detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const messages = detail
        .map((d) => (d && typeof d === "object" ? (d as { msg?: unknown }).msg : null))
        .filter((m): m is string => typeof m === "string");
      if (messages.length > 0) return new ApiError(messages.join("; "), status, "VALIDATION_FAILED");
    }
    if (typeof detail === "string") return new ApiError(detail, status, null);
  }
  // An empty 5xx is a proxy (npm run dev) with no API behind it, not the API itself.
  if (status >= 500 && (body === null || body === "")) {
    return new ApiError(
      "The Interlock API isn't answering. Start it with .\\scripts\\start-interlock.ps1.",
      status,
      "NETWORK",
    );
  }
  return new ApiError(`Request failed (HTTP ${status}).`, status, null);
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  headers: Record<string, string> = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers: body === undefined ? headers : { "Content-Type": "application/json", ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("Can't reach the Interlock API. Is it running?", 0, "NETWORK");
  }
  const text = await response.text();
  const parsed: unknown = text ? safeJson(text) : null;
  if (!response.ok) throw errorFromBody(response.status, parsed);
  return parsed as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export const api = {
  config: () => request<UiConfig>("GET", "/config/ui"),
  status: () => request<WhatsAppStatus>("GET", "/whatsapp/status"),

  reviews: (params: { localDate?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.localDate) q.set("local_date", params.localDate);
    if (params.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return request<ApprovalRequest[]>("GET", `/approval-requests${qs ? `?${qs}` : ""}`);
  },
  review: async (id: string): Promise<ReviewDetail> => {
    const detail = await request<S["ReviewDetailOut"]>("GET", `/approval-requests/${id}`);
    return {
      ...detail,
      shares: (detail.shares ?? []).map((s) => ({ ...s, has_image: s.has_image ?? false })),
    };
  },
  createManualReview: () => request<ApprovalRequest>("POST", "/approval-requests"),
  openReview: (id: string) => request<ApprovalRequest>("POST", `/approval-requests/${id}/open`),
  preview: async (id: string): Promise<Preview> => {
    const p = await request<S["PreviewResponse"]>("POST", `/approval-requests/${id}/preview`);
    return { ...p, has_image: p.has_image ?? false };
  },
  commit: (id: string, body: CommitRequest, idempotencyKey: string) =>
    request<CommitResponse>("POST", `/approval-requests/${id}/commit`, body, {
      "Idempotency-Key": idempotencyKey,
    }),
  retryRecipient: (recipientId: string) =>
    request<ShareRecipient>("POST", `/shares/recipients/${recipientId}/retry`),

  groups: () => request<Group[]>("GET", "/whatsapp/groups"),
  resolveGroup: (name: string) =>
    request<GroupCandidate[]>("POST", "/whatsapp/groups/resolve", { name }),
  addGroup: (body: GroupCreate) => request<Group>("POST", "/whatsapp/groups", body),
  setGroupEnabled: (id: string, enabled: boolean) =>
    request<Group>("PATCH", `/whatsapp/groups/${id}/enabled?enabled=${enabled}`),
};
