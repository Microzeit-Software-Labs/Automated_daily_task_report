/** Loads and prepares apps/agent/sql/agent_queries.sql -- the shared
 * contract with the Python test harness (tests/integration/
 * test_local_agent_provider.py's FakeAgent parses and runs the exact same
 * file). Pure string handling, no database -- kept separate from db.ts so
 * it can be unit-tested (test/sqlNamedQueries.test.ts) without a Postgres
 * connection.
 */
import { readFileSync } from "node:fs";

const NAME_HEADER = /^-- name:\s*(\w+)\s*$/;
// A ":" preceded by another ":" is a Postgres cast ("::interval", "::jsonb"),
// never a parameter.
const PLACEHOLDER = /(?<!:):(\w+)/g;
const COMMENT_LINE = /^\s*--/;

export function loadNamedQueries(sqlFileContents: string): Record<string, string> {
  const blocks: Record<string, string[]> = {};
  let current: string | null = null;

  for (const line of sqlFileContents.split("\n")) {
    const match = NAME_HEADER.exec(line);
    if (match) {
      current = match[1] as string;
      blocks[current] = [];
      continue;
    }
    // Comment lines are documentation, not SQL: dropping them keeps a
    // ":name" or "::cast" mentioned in prose from being read as a parameter.
    if (current !== null && !COMMENT_LINE.test(line)) {
      (blocks[current] as string[]).push(line);
    }
  }

  const result: Record<string, string> = {};
  for (const [name, lines] of Object.entries(blocks)) {
    result[name] = lines.join("\n").trim();
  }
  return result;
}

export function loadNamedQueriesFromFile(path: string): Record<string, string> {
  return loadNamedQueries(readFileSync(path, "utf-8"));
}

export interface PositionalQuery {
  text: string;
  values: unknown[];
}

/** Converts ":name" placeholders (SQLAlchemy's own bind-parameter syntax,
 * which agent_queries.sql uses so the Python side can run it unmodified via
 * text()) into pg's positional "$1, $2, ...", building the matching values
 * array in the same pass. The same named parameter used twice in one
 * statement reuses one positional slot, matching how a real driver bind
 * would behave. */
export function toPositionalQuery(sql: string, params: Record<string, unknown>): PositionalQuery {
  const values: unknown[] = [];
  const seen = new Map<string, number>();

  const text = sql.replace(PLACEHOLDER, (_match, name: string) => {
    if (!(name in params)) {
      throw new Error(`missing parameter "${name}" for this statement`);
    }
    let index = seen.get(name);
    if (index === undefined) {
      values.push(params[name]);
      index = values.length;
      seen.set(name, index);
    }
    return `$${index}`;
  });

  return { text, values };
}
