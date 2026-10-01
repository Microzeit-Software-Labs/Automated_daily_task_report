import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "./api";
import { PromptHost } from "./components/PromptHost";
import { ToastProvider } from "./components/Toast";
import { WhatsAppBanner } from "./components/WhatsAppBanner";
import { WhatsAppLinkModal } from "./components/WhatsAppLinkModal";
import { WhatsAppPill } from "./components/WhatsAppPill";
import { Dashboard } from "./pages/Dashboard";
import { Groups } from "./pages/Groups";
import { Review } from "./pages/Review";
import { Settings } from "./pages/Settings";
import { href, useRoute } from "./router";
import { ErrorNote } from "./ui";

export function App() {
  return (
    <ToastProvider>
      <Shell />
    </ToastProvider>
  );
}

function Shell() {
  const route = useRoute();
  const config = useQuery({ queryKey: ["config"], queryFn: api.config, staleTime: Infinity });
  // One shared status query: the pill, the banner and Settings all read it.
  const status = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 15_000 });
  const [linking, setLinking] = useState(false);

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href={href({ name: "dashboard" })}>
          Interlock
        </a>
        <nav className="nav">
          <a
            href={href({ name: "dashboard" })}
            aria-current={route.name === "dashboard" || route.name === "review" ? "page" : undefined}
          >
            Reports
          </a>
          <a href={href({ name: "groups" })} aria-current={route.name === "groups" ? "page" : undefined}>
            Groups
          </a>
          <a href={href({ name: "settings" })} aria-current={route.name === "settings" ? "page" : undefined}>
            Settings
          </a>
        </nav>
        {config.data && <WhatsAppPill config={config.data} status={status.data} failed={status.isError} />}
      </header>
      <WhatsAppBanner status={status.data} onLink={() => setLinking(true)} />
      <main className="main">
        {config.error ? (
          <ErrorNote error={config.error} />
        ) : !config.data ? (
          <p className="muted">Loading…</p>
        ) : route.name === "review" ? (
          <Review id={route.id} config={config.data} />
        ) : route.name === "groups" ? (
          <div className="page page-narrow">
            <Groups />
          </div>
        ) : route.name === "settings" ? (
          <Settings config={config.data} status={status.data} onLink={() => setLinking(true)} />
        ) : (
          <Dashboard config={config.data} />
        )}
      </main>
      {config.data && <PromptHost config={config.data} />}
      {linking && <WhatsAppLinkModal status={status.data} onClose={() => setLinking(false)} />}
    </div>
  );
}
