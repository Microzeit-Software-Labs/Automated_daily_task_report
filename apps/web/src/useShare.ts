// Everything "share this report" needs, in one place -- used by the report
// page and by the 09:00 / 17:00 popup, so both behave identically: same
// recipient defaults, same preview polling, and the same commit (with its
// idempotency key and the preview's content hash, so what is on screen is
// what goes out).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, ApiError, type ApprovalRequest, type UiConfig } from "./api";
import { defaultRecipientIds, resolveSendAt, type SendChoice } from "./logic";

export function useShare({
  review,
  config,
  priorShares = 0,
}: {
  review: Pick<ApprovalRequest, "id" | "kind">;
  config: UiConfig;
  /** Shares this review already produced; the next commit is one version up. */
  priorShares?: number;
}) {
  const queryClient = useQueryClient();
  const id = review.id;

  const groups = useQuery({ queryKey: ["groups"], queryFn: api.groups });
  const preview = useQuery({
    queryKey: ["preview", id],
    queryFn: () => api.preview(id),
    // Follows the sheet import, which can change tasks every minute. The
    // commit carries this preview's hash, so what's on screen is what sends.
    refetchInterval: 60_000,
  });
  const status = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 15_000 });

  // "Your tasks changed" -- the preview's data fingerprint moved while it was open.
  const [changedNotice, setChangedNotice] = useState(false);
  const lastHash = useRef<string | null>(null);
  useEffect(() => {
    const hash = preview.data?.content_hash;
    if (!hash) return;
    if (lastHash.current !== null && lastHash.current !== hash) setChangedNotice(true);
    lastHash.current = hash;
  }, [preview.data?.content_hash]);

  const all = groups.data ?? [];
  const enabled = all.filter((g) => g.enabled);
  const [selected, setSelected] = useState<Set<string> | null>(null);
  useEffect(() => {
    if (selected === null && all.length > 0) {
      setSelected(new Set(defaultRecipientIds(review.kind, all)));
    }
  }, [all, selected, review.kind]);
  const chosen = enabled.filter((g) => selected?.has(g.id));
  const toggle = (groupId: string, on: boolean) => {
    const next = new Set(selected ?? []);
    if (on) next.add(groupId);
    else next.delete(groupId);
    setSelected(next);
  };

  const [when, setWhen] = useState<SendChoice>({ kind: "now" });
  const sendAt = resolveSendAt(when, config.timezone);

  const refresh = () => {
    for (const key of ["review", "reviews", "prompt"]) {
      void queryClient.invalidateQueries({ queryKey: key === "review" ? [key, id] : [key] });
    }
  };

  const share = useMutation({
    mutationFn: (choice: SendChoice) => {
      if (!preview.data) throw new Error("The preview hasn't loaded yet.");
      const resolved = resolveSendAt(choice, config.timezone);
      if ("error" in resolved) throw new Error(resolved.error);
      return api.commit(
        id,
        {
          mode: "SHARE_ONLY",
          action_version: priorShares + 1,
          recipient_group_ids: chosen.map((g) => g.id),
          send_at: resolved.sendAt,
          delay_minutes: resolved.delayMinutes,
          expected_content_hash: preview.data.content_hash,
        },
        crypto.randomUUID(),
      );
    },
    onSuccess: refresh,
    onError: (error) => {
      if (error instanceof ApiError && error.code === "DATASET_VERSION_CONFLICT") {
        void preview.refetch();
      }
    },
  });

  const skip = useMutation({
    mutationFn: () =>
      api.commit(id, { mode: "UPDATE_ONLY", action_version: 1 }, crypto.randomUUID()),
    onSuccess: refresh,
  });

  const busy = share.isPending || skip.isPending;
  return {
    groups,
    enabled,
    chosen,
    isSelected: (groupId: string) => selected?.has(groupId) ?? false,
    toggle,
    preview,
    status,
    changedNotice,
    dismissChanged: () => {
      setChangedNotice(false);
      share.reset();
    },
    /** The last share failed only because the tasks changed after the preview. */
    drifted: share.error instanceof ApiError && share.error.code === "DATASET_VERSION_CONFLICT",
    when,
    setWhen,
    sendAt,
    share,
    skip,
    busy,
    /** Preview loaded, at least one group chosen, nothing in flight. */
    ready: !!preview.data && chosen.length > 0 && !busy,
  };
}
