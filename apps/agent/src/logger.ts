/** Always writes to apps/agent/logs/agent.log -- the day-to-day run mode is a
 * Windows Scheduled Task with no visible console (see
 * docs/whatsapp-agent-setup.md), so the file is the only place those lines
 * are ever seen. When started from a real terminal (first-run pairing,
 * debugging), also echoes to stdout: otherwise a startup failure would leave
 * a blank window and the reason buried in a file nobody is looking at yet.
 */
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import pino from "pino";

const LOG_DIR = join(__dirname, "..", "..", "logs");
mkdirSync(LOG_DIR, { recursive: true });

// sync: an async destination is still opening its file when a startup
// failure calls process.exit(), so the one line that explains the failure is
// lost (and pino throws "sonic boom is not ready yet" on the way out). This
// agent writes a few lines a minute; synchronous writes cost nothing here.
const streams: pino.StreamEntry[] = [
  { stream: pino.destination({ dest: join(LOG_DIR, "agent.log"), mkdir: true, sync: true }) },
];
if (process.stdout.isTTY) {
  streams.push({ stream: process.stdout });
}

export const logger = pino({ level: process.env.LOG_LEVEL ?? "info" }, pino.multistream(streams));
