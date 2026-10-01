import type { SheetSource } from "../api";
import { sheetBanner } from "../logic";
import { useNow } from "../useNow";

/** A page-wide notice when the Google Sheet can't be read, with the one button
 * that fixes it. Silent while the sheet is fine, and while a new link is still
 * being read. Same look and place as the WhatsApp banner. */
export function SheetBanner({
  sheet,
  tz,
  onFix,
}: {
  sheet: SheetSource | undefined;
  tz: string;
  onFix: () => void;
}) {
  const now = useNow(60_000);
  const info = sheetBanner(sheet, tz, now);
  if (!info) return null;

  return (
    <div className={`banner banner-${info.tone}`} role="alert">
      <div className="banner-text">
        <strong>{info.title}</strong>
        <span>{info.message}</span>
      </div>
      {info.action && (
        <button className="btn" onClick={onFix}>
          {info.actionLabel}
        </button>
      )}
    </div>
  );
}
