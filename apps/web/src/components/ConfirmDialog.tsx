import type { ReactNode } from "react";

import { Modal } from "./Modal";

/** "Are you sure?" for the one action that can't be undone from the UI:
 * closing a report without sharing it. The safe choice has the focus. */
export function ConfirmDialog({
  title,
  confirmLabel,
  cancelLabel = "Keep it",
  busy = false,
  onConfirm,
  onCancel,
  children,
}: {
  title: string;
  confirmLabel: string;
  cancelLabel?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  children: ReactNode;
}) {
  return (
    <Modal title={title} onClose={onCancel}>
      <div className="confirm-body">{children}</div>
      <div className="actions">
        <button className="btn" data-autofocus onClick={onCancel} disabled={busy}>
          {cancelLabel}
        </button>
        <button className="btn btn-danger" onClick={onConfirm} disabled={busy}>
          {busy ? "Working…" : confirmLabel}
        </button>
      </div>
    </Modal>
  );
}
