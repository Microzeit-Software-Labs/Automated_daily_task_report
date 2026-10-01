import { type ReactNode, useEffect, useId, useRef } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** A centred dialog over a dimmed page. Escape calls `onClose`, Tab stays
 * inside, and focus returns to where it was when the dialog goes away. */
export function Modal({
  title,
  icon,
  onClose,
  closeLabel = "Close",
  wide = false,
  children,
}: {
  title: string;
  icon?: ReactNode;
  onClose?: () => void;
  /** Accessible name and tooltip of the × button. */
  closeLabel?: string;
  /** For content that needs the room, like a report image. */
  wide?: boolean;
  children: ReactNode;
}) {
  const titleId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const first = dialog.current?.querySelector<HTMLElement>("[data-autofocus]");
    (first ?? dialog.current)?.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && onCloseRef.current) {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab" || !dialog.current) return;
      const items = [...dialog.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
      if (items.length === 0) return;
      const head = items[0]!;
      const tail = items[items.length - 1]!;
      if (event.shiftKey && document.activeElement === head) {
        event.preventDefault();
        tail.focus();
      } else if (!event.shiftKey && document.activeElement === tail) {
        event.preventDefault();
        head.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, []);

  return (
    <div className="modal-backdrop">
      <div ref={dialog} className={wide ? "modal modal-wide" : "modal"} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}>
        <header className="modal-head">
          {icon && <span className="modal-icon" aria-hidden="true">{icon}</span>}
          <h2 id={titleId}>{title}</h2>
          {onClose && (
            <button className="modal-close" onClick={onClose} aria-label={closeLabel} title={closeLabel}>
              ×
            </button>
          )}
        </header>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

export function BellIcon() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 9a6 6 0 0 1 12 0c0 6 2 7.5 2 7.5H4S6 15 6 9Z" />
      <path d="M10 20a2 2 0 0 0 4 0" />
    </svg>
  );
}
