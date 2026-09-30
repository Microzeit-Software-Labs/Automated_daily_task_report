import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type GroupCandidate } from "../api";
import { ErrorNote } from "../ui";

export function Groups() {
  const queryClient = useQueryClient();
  const groups = useQuery({ queryKey: ["groups"], queryFn: api.groups });
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => api.setGroupEnabled(id, enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["groups"] }),
  });

  return (
    <div className="stack">
      <section className="card">
        <h1>WhatsApp groups</h1>
        <p className="muted">
          Reports only ever go to groups pinned here. Each is pinned to one exact WhatsApp group, so a rename or a
          look-alike group can't redirect a report.
        </p>
        {groups.error ? (
          <ErrorNote error={groups.error} />
        ) : groups.isPending ? (
          <p className="muted">Loading…</p>
        ) : groups.data.length === 0 ? (
          <p className="empty">No groups yet. Add one below — start with a test group that only has you in it.</p>
        ) : (
          <ul className="rows">
            {groups.data.map((g) => (
              <li key={g.id} className="row row-static">
                <span className="row-main">
                  <strong className={g.enabled ? "" : "muted"}>{g.display_name}</strong>
                  <span className="muted small">
                    {[g.default_morning && "morning default", g.default_evening && "evening default", g.description]
                      .filter(Boolean)
                      .join(" · ") || "not preselected"}
                  </span>
                </span>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={g.enabled}
                    disabled={toggle.isPending}
                    onChange={(e) => toggle.mutate({ id: g.id, enabled: e.target.checked })}
                  />
                  Enabled
                </label>
              </li>
            ))}
          </ul>
        )}
        {toggle.error && <ErrorNote error={toggle.error} />}
      </section>
      <AddGroup />
    </div>
  );
}

function AddGroup() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [picked, setPicked] = useState<GroupCandidate | null>(null);
  const [morning, setMorning] = useState(false);
  const [evening, setEvening] = useState(false);
  const [description, setDescription] = useState("");

  const resolve = useMutation({ mutationFn: api.resolveGroup, onSuccess: () => setPicked(null) });
  const add = useMutation({
    mutationFn: api.addGroup,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["groups"] });
      setName("");
      setPicked(null);
      setDescription("");
      setMorning(false);
      setEvening(false);
      resolve.reset();
    },
  });

  return (
    <section className="card">
      <h2>Add a group</h2>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) resolve.mutate(name.trim());
        }}
      >
        <input
          className="input"
          placeholder="Group name as it appears in WhatsApp"
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-label="Group name"
        />
        <button className="btn" disabled={!name.trim() || resolve.isPending}>
          {resolve.isPending ? "Looking…" : "Find"}
        </button>
      </form>
      <p className="muted small">Needs the WhatsApp agent running and paired; the lookup can take a few seconds.</p>
      {resolve.error && <ErrorNote error={resolve.error} />}

      {resolve.data &&
        (resolve.data.length === 0 ? (
          <p className="empty">No group matches “{resolve.variables}”. Check the spelling.</p>
        ) : (
          <fieldset className="candidates">
            <legend>Pick the exact group</legend>
            {resolve.data.map((c) => (
              <label key={c.external_jid} className="check candidate">
                <input
                  type="radio"
                  name="candidate"
                  checked={picked?.external_jid === c.external_jid}
                  onChange={() => setPicked(c)}
                />
                <span>
                  <strong>{c.display_name}</strong>
                  <span className="muted small">
                    {" "}
                    · {c.member_count ?? "?"} members · {Math.round(c.confidence * 100)}% match
                  </span>
                </span>
              </label>
            ))}
          </fieldset>
        ))}

      {picked && (
        <div className="stack-tight">
          <input
            className="input"
            placeholder="Note (optional), e.g. “managers”"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            aria-label="Note"
          />
          <label className="check">
            <input type="checkbox" checked={morning} onChange={(e) => setMorning(e.target.checked)} />
            Preselect for morning reports
          </label>
          <label className="check">
            <input type="checkbox" checked={evening} onChange={(e) => setEvening(e.target.checked)} />
            Preselect for end-of-day and manual reports
          </label>
          {add.error && <ErrorNote error={add.error} />}
          <div className="actions">
            <button
              className="btn btn-primary"
              disabled={add.isPending}
              onClick={() =>
                add.mutate({
                  display_name: picked.display_name,
                  external_jid: picked.external_jid,
                  description,
                  default_morning: morning,
                  default_evening: evening,
                })
              }
            >
              Pin “{picked.display_name}”
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
