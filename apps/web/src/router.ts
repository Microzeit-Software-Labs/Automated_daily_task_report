// Hash routing: the API serves one static index.html at /app/, so every
// screen lives after the "#" and a refresh never 404s.
import { useSyncExternalStore } from "react";

export type Route =
  | { name: "dashboard" }
  | { name: "review"; id: string }
  | { name: "groups" };

export function parseRoute(hash: string): Route {
  const path = hash.replace(/^#/, "");
  const review = /^\/review\/([^/]+)$/.exec(path);
  if (review?.[1]) return { name: "review", id: decodeURIComponent(review[1]) };
  if (path === "/groups") return { name: "groups" };
  return { name: "dashboard" };
}

export function href(route: Route): string {
  if (route.name === "review") return `#/review/${encodeURIComponent(route.id)}`;
  if (route.name === "groups") return "#/groups";
  return "#/";
}

export function navigate(route: Route): void {
  window.location.hash = href(route);
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener("hashchange", onChange);
  return () => window.removeEventListener("hashchange", onChange);
}

export function useRoute(): Route {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash);
  return parseRoute(hash);
}
