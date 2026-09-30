import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, type LinkStatus, type WhatsAppStatus } from "../api";
import { linkView } from "../logic";
import { ErrorNote } from "../ui";
import { Modal } from "./Modal";

/** Link (or re-link, or change) the WhatsApp phone by scanning a QR code.
 *
 * Changing phone while connected asks first, because it ends the current
 * link. The current link keeps working the whole time the QR is on screen:
 * the agent pairs in a scratch session and only switches over once the new
 * phone has fully linked, so cancelling or letting the code expire changes
 * nothing.
 */
export function WhatsAppLinkModal({
  status,
  onClose,
}: {
  status: WhatsAppStatus | undefined;
  onClose: () => void;
}) {
  const connected = status?.state === "CONNECTED";
  const [confirmed, setConfirmed] = useState(!connected);

  if (!confirmed) {
    return (
      <Modal title="Link a different phone?" onClose={onClose}>
        <p>
          WhatsApp is connected as <strong>{status?.account_number ?? "this phone"}</strong>.
        </p>
        <ul className="plain-list">
          <li>The current phone stays connected until the new one is linked, so nothing is interrupted.</li>
          <li>Once the new phone is linked, this one is disconnected.</li>
          <li>The new number must be a member of your WhatsApp groups, or reports can't reach them.</li>
        </ul>
        <div className="actions">
          <button className="btn btn-quiet" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" data-autofocus onClick={() => setConfirmed(true)}>
            Show QR code
          </button>
        </div>
      </Modal>
    );
  }
  return <LinkFlow onClose={onClose} />;
}

function LinkFlow({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [startedAt, setStartedAt] = useState(() => Date.now());
  const [, tick] = useState(0);

  const start = useMutation({
    mutationFn: api.startLink,
    onMutate: () => queryClient.removeQueries({ queryKey: ["link"] }),
    onSuccess: () => setStartedAt(Date.now()),
  });
  const began = useRef(false);
  useEffect(() => {
    if (began.current) return; // StrictMode runs effects twice in development
    began.current = true;
    start.mutate();
  }, [start]);

  const link = useQuery({
    queryKey: ["link"],
    queryFn: api.link,
    refetchInterval: 1_500,
    enabled: start.isSuccess,
  });
  // Only this attempt's progress: a previous attempt's "succeeded" must never
  // flash up while a new code is being prepared.
  const attemptId = start.data?.pairing_id ?? null;
  const current: LinkStatus | undefined =
    link.data && (attemptId === null || link.data.pairing_id === attemptId) ? link.data : undefined;

  useEffect(() => {
    const timer = setInterval(() => tick((n) => n + 1), 1_000);
    return () => clearInterval(timer);
  }, []);
  const view = start.isError ? "failed" : linkView(current, Date.now() - startedAt);

  // After the switch-over, wait for the connection itself to come up.
  const status = useQuery({
    queryKey: ["status"],
    queryFn: api.status,
    refetchInterval: view === "success" ? 2_000 : 15_000,
  });
  const nowConnected = status.data?.state === "CONNECTED";

  const finishedForGood = view === "success" && nowConnected;
  const close = () => {
    // Abandon an attempt that hasn't been scanned yet. Once scanned, let it finish.
    if (view === "preparing" || view === "scan") void api.cancelLink().catch(() => undefined);
    void queryClient.invalidateQueries({ queryKey: ["status"] });
    onClose();
  };
  const retry = () => {
    start.reset();
    start.mutate();
  };

  return (
    <Modal title="Connect WhatsApp" onClose={close}>
      {view === "preparing" && <p className="muted">Preparing the QR code…</p>}

      {view === "scan" && current && (
        <>
          <ol className="steps">
            <li>Open WhatsApp on your phone.</li>
            <li>
              Go to <strong>Settings → Linked Devices → Link a Device</strong>.
            </li>
            <li>Scan this code.</li>
          </ol>
          <div className="qr-box">
            <img
              key={current.qr_version ?? "qr"}
              src={`/whatsapp/link/qr.svg?v=${encodeURIComponent(current.qr_version ?? "")}`}
              width={240}
              height={240}
              alt="WhatsApp QR code"
            />
          </div>
          <p className="muted small center">Waiting for connection… The code refreshes by itself.</p>
        </>
      )}

      {view === "scanned" && <p className="prompt-done">Scanned. Finishing the link…</p>}

      {view === "success" && !nowConnected && (
        <p className="prompt-done">Linked. Connecting…</p>
      )}
      {finishedForGood && (
        <p className="prompt-done">
          ✓ WhatsApp connected
          {status.data?.account_number ? ` as ${status.data.account_number}` : ""}.
        </p>
      )}

      {view === "expired" && (
        <>
          <div className="note note-warn" role="status">
            <strong>The code expired</strong>
            <span>Nobody scanned it in time. Nothing has changed.</span>
          </div>
          <div className="actions">
            <button className="btn btn-primary" onClick={retry}>
              Get a new QR code
            </button>
          </div>
        </>
      )}

      {view === "failed" && (
        <>
          {start.isError ? (
            <ErrorNote error={start.error} />
          ) : (
            <div className="note note-stop" role="alert">
              <strong>Couldn't link</strong>
              <span>{current?.detail || "Linking didn't start. Try again."}</span>
            </div>
          )}
          <div className="actions">
            <button className="btn btn-primary" onClick={retry}>
              Try again
            </button>
          </div>
        </>
      )}

      {view === "offline" && (
        <>
          <div className="note note-warn" role="status">
            <strong>The WhatsApp service isn't running</strong>
            <span>There's no QR code to show until Interlock's WhatsApp service is running. Start Interlock, then try again.</span>
          </div>
          <div className="actions">
            <button className="btn btn-primary" onClick={retry}>
              Try again
            </button>
          </div>
        </>
      )}

      {(view === "success" && finishedForGood) || view === "scanned" ? (
        <div className="actions">
          <button className="btn btn-primary" data-autofocus onClick={close}>
            {finishedForGood ? "Done" : "Close"}
          </button>
        </div>
      ) : null}
      {(view === "scan" || view === "preparing") && (
        <div className="actions">
          <button className="btn btn-quiet" onClick={close}>
            Cancel
          </button>
        </div>
      )}
    </Modal>
  );
}
