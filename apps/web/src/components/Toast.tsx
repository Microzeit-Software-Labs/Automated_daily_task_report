import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";

import type { ResultTone } from "../logic";
import { Icon } from "../ui";

interface ToastItem {
  id: number;
  message: string;
  tone: ResultTone;
}

const ToastContext = createContext<{ show: (message: string, tone?: ResultTone) => void }>({
  show: () => undefined,
});

/** Lets any page say "Done" or "That didn't work" without a dialog. Toasts
 * clear themselves after a few seconds; the live region announces them. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const show = useCallback((message: string, tone: ResultTone = "go") => {
    setItems((current) => [...current.slice(-2), { id: Date.now() + Math.random(), message, tone }]);
  }, []);
  const value = useMemo(() => ({ show }), [show]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((item) => (
          <ToastView key={item.id} item={item} onDone={() => setItems((c) => c.filter((t) => t.id !== item.id))} />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

function ToastView({ item, onDone }: { item: ToastItem; onDone: () => void }) {
  useEffect(() => {
    const timer = setTimeout(onDone, 5_000);
    return () => clearTimeout(timer);
  }, [onDone]);
  return (
    <div className={`toast toast-${item.tone}`}>
      <Icon name={item.tone === "stop" ? "alert" : "check"} />
      <span>{item.message}</span>
      <button className="toast-close" onClick={onDone} aria-label="Dismiss">
        ×
      </button>
    </div>
  );
}

export function useToast() {
  return useContext(ToastContext);
}
