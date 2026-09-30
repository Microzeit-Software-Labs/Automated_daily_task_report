import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, type WhatsAppStatus } from "../api";
import { whatsappBanner } from "../logic";

/** A page-wide notice when WhatsApp can't send, with the one button that
 * fixes it. Silent while everything is fine, and while it is merely
 * reconnecting by itself (the status pill covers that). */
export function WhatsAppBanner({
  status,
  onLink,
}: {
  status: WhatsAppStatus | undefined;
  onLink: () => void;
}) {
  const queryClient = useQueryClient();
  const reconnect = useMutation({
    mutationFn: api.reconnect,
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["status"] }),
  });
  const info = whatsappBanner(status);
  if (!info) return null;

  return (
    <div className={`banner banner-${info.tone}`} role="alert">
      <div className="banner-text">
        <strong>{info.title}</strong>
        <span>{info.message}</span>
        {reconnect.error && <span className="field-error">{reconnect.error.message}</span>}
      </div>
      {info.action && (
        <button
          className="btn"
          disabled={reconnect.isPending}
          onClick={() => (info.action === "link" ? onLink() : reconnect.mutate())}
        >
          {reconnect.isPending ? "Reconnecting…" : info.actionLabel}
        </button>
      )}
    </div>
  );
}
