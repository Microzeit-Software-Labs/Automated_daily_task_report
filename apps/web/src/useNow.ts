import { useEffect, useState } from "react";

/** The current time, refreshed every `everyMs`, so "in 25 min" and
 * "Scheduled 17:30" stay true while a page sits open. */
export function useNow(everyMs = 30_000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), everyMs);
    return () => clearInterval(timer);
  }, [everyMs]);
  return now;
}
