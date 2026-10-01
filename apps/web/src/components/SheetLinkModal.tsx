import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, type SheetSource } from "../api";
import { looksLikeSheetLink, plural, sheetProgress } from "../logic";
import { Callout, ErrorNote, Icon } from "../ui";
import { useNow } from "../useNow";
import { Modal } from "./Modal";

/** Connect, change or fix the Google Sheet the tasks are read from.
 *
 * It works the way linking a WhatsApp phone does: look before you switch, and a
 * failed attempt changes nothing. The pasted link is checked first (nothing is
 * saved by that), you see what it holds, and only then does it become the sheet
 * in use. The dialog then waits for the worker's first read of it. */
export function SheetLinkModal({
  sheet,
  onClose,
}: {
  sheet: SheetSource | undefined;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [text, setText] = useState(sheet?.last_error && sheet.url ? sheet.url : "");
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const now = useNow(2_000);

  const check = useMutation({ mutationFn: api.checkSheet });
  const save = useMutation({
    mutationFn: api.saveSheet,
    onSuccess: (saved) => {
      // Start from the fresh state (importing), never from the previous sheet's
      // "last read", or the dialog would call the switch done before it began.
      queryClient.setQueryData(["sheet"], saved);
      setStartedAt(Date.now());
    },
  });
  // The shared status query, polled quickly while the worker reads the new sheet.
  const live = useQuery({
    queryKey: ["sheet"],
    queryFn: api.sheet,
    refetchInterval: startedAt === null ? false : 2_000,
  });

  const progress = startedAt === null ? null : sheetProgress(live.data, startedAt, now.getTime());
  useEffect(() => {
    if (progress !== "done") return;
    for (const key of ["reviews", "review", "preview", "config"]) {
      void queryClient.invalidateQueries({ queryKey: [key] });
    }
  }, [progress, queryClient]);

  const result = check.data;
  const checked = result?.ok ? result : null;
  const changing = !!sheet?.configured;
  const editing = progress === null;

  const edit = (value: string) => {
    setText(value);
    check.reset();
    save.reset();
  };
  const startOver = () => {
    setStartedAt(null);
    check.reset();
    save.reset();
  };

  return (
    <Modal title={changing ? "Change the Google Sheet" : "Connect a Google Sheet"} onClose={onClose}>
      {editing && !checked && (
        <>
          <p className="muted">Interlock reads your tasks from a Google Sheet. It only reads it and never changes it.</p>
          <ol className="steps">
            <li>Open your sheet in the browser.</li>
            <li>
              Press <strong>Share</strong>, and under General access choose <strong>Anyone with the link</strong> (the{" "}
              <strong>Viewer</strong> role).
            </li>
            <li>Copy the link from the address bar and paste it here.</li>
          </ol>
          <div className="inline-form sheet-form">
            <input
              className="input"
              data-autofocus
              type="url"
              inputMode="url"
              placeholder="https://docs.google.com/spreadsheets/d/…"
              aria-label="Link to your Google Sheet"
              value={text}
              onChange={(e) => edit(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && looksLikeSheetLink(text) && !check.isPending) check.mutate(text.trim());
              }}
            />
            <button
              className="btn btn-primary"
              disabled={!looksLikeSheetLink(text) || check.isPending}
              onClick={() => check.mutate(text.trim())}
            >
              {check.isPending ? "Checking…" : "Check"}
            </button>
          </div>
          {text.trim() !== "" && !looksLikeSheetLink(text) && (
            <p className="field-error">That doesn't look like a Google Sheets link yet.</p>
          )}
          {check.error && <ErrorNote error={check.error} />}
          {result && !result.ok && result.problem && (
            <div className="sheet-problem">
              <Callout tone="stop" title="This sheet can't be used">
                {result.problem.message}
              </Callout>
            </div>
          )}
          {changing && (
            <p className="muted small">Your current sheet keeps working until a new one has been read successfully.</p>
          )}
        </>
      )}

      {editing && checked && (
        <>
          <Callout tone="go" title={`Found ${plural(checked.task_count, "task")}`}>
            {checked.tab && checked.tab !== "0" ? `Reading tab ${checked.tab} of the sheet.` : "Reading the first tab of the sheet."}
          </Callout>
          {checked.sample_titles.length > 0 && (
            <>
              <h3>Most recent tasks</h3>
              <ul className="plain-list">
                {checked.sample_titles.map((title) => (
                  <li key={title}>{title}</li>
                ))}
              </ul>
            </>
          )}
          {checked.skipped > 0 && (
            <p className="muted small">
              {plural(checked.skipped, "row")} will be skipped (no Sr No. or task, or a repeated Sr No.).
            </p>
          )}
          {checked.same_sheet ? (
            <div className="note note-warn">
              <strong>This is the sheet you already use.</strong>
              <span>Saving reads it again now.</span>
            </div>
          ) : (
            checked.will_hide > 0 && (
              <div className="note note-warn" role="status">
                <strong>
                  {plural(checked.will_hide, "task")} from your current sheet will be hidden from reports.
                </strong>
                <span>They aren't deleted: switch back to that sheet any time and they return.</span>
              </div>
            )
          )}
          {save.error && <ErrorNote error={save.error} />}
          <div className="actions">
            <button className="btn btn-ghost" disabled={save.isPending} onClick={() => check.reset()}>
              Check another link
            </button>
            <button className="btn btn-primary" disabled={save.isPending} onClick={() => save.mutate(text.trim())}>
              {save.isPending ? "Saving…" : checked.same_sheet ? "Read it again" : "Use this sheet"}
            </button>
          </div>
        </>
      )}

      {(progress === "importing" || progress === "slow") && (
        <>
          <p className="prompt-done" role="status">
            Reading your sheet…
          </p>
          <p className="muted small">This usually takes under a minute. You can close this: it carries on.</p>
          {progress === "slow" && (
            <div className="note note-warn" role="status">
              <strong>It's taking longer than usual</strong>
              <span>
                The Interlock service reads the sheet. If it isn't running, start it with{" "}
                <code>.\scripts\start-interlock.ps1</code>.
              </span>
            </div>
          )}
          <div className="actions">
            <button className="btn" onClick={onClose}>
              Close
            </button>
          </div>
        </>
      )}

      {progress === "done" && (
        <>
          <p className="prompt-done" role="status" data-autofocus tabIndex={-1}>
            <Icon name="check" /> {plural(live.data?.last_task_count ?? 0, "task")} imported. Reports now use this sheet.
          </p>
          <div className="actions">
            <button className="btn btn-primary" onClick={onClose}>
              Done
            </button>
          </div>
        </>
      )}

      {progress === "failed" && (
        <>
          <Callout tone="stop" title="The sheet was saved, but it couldn't be read">
            {live.data?.last_error}
          </Callout>
          <p className="muted small">Your earlier tasks are still in your reports. Fix the problem, or try another link.</p>
          <div className="actions">
            <button className="btn" onClick={onClose}>
              Close
            </button>
            <button className="btn btn-primary" onClick={startOver}>
              Try another link
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}
