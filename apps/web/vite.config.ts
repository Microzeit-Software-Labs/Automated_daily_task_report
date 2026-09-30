import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Served by the API at /app/ in real use. `npm run dev` proxies API calls to
// a locally running uvicorn so the UI can be worked on with hot reload.
const API = "http://127.0.0.1:8000";
const apiPaths = ["/approval-requests", "/whatsapp", "/shares", "/tasks", "/config", "/readiness", "/health"];

export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: { proxy: Object.fromEntries(apiPaths.map((p) => [p, API])) },
  test: { include: ["test/**/*.test.ts"] },
});
