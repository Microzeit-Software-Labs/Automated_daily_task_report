// "Close" on a report that is waiting for approval: the existing UPDATE_ONLY
// commit, which ends it as "Not shared". The Reports list and the report page
// both use this, so closing means the same thing in both.

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "./api";

export function useCloseReport() {
  const queryClient = useQueryClient();
  return useMutation({
    // An open report has no earlier share, so this is always version 1.
    mutationFn: (reviewId: string) =>
      api.commit(reviewId, { mode: "UPDATE_ONLY", action_version: 1 }, crypto.randomUUID()),
    onSuccess: (_result, reviewId) => {
      void queryClient.invalidateQueries({ queryKey: ["reviews"] });
      void queryClient.invalidateQueries({ queryKey: ["review", reviewId] });
      void queryClient.invalidateQueries({ queryKey: ["prompt"] });
    },
  });
}
