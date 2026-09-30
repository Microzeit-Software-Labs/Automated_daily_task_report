import { whatsappSegments } from "../logic";

/** The message as WhatsApp will show it: the table image (if any) with the
 * text as its caption, or just the text. */
export function Report({ text, imageUrl }: { text: string; imageUrl: string | null }) {
  if (!imageUrl) return <Bubble text={text} />;
  return (
    <div className="bubble bubble-image">
      <a href={imageUrl} target="_blank" rel="noreferrer" title="Open full size">
        <img src={imageUrl} alt="Report table that will be sent" className="report-image" />
      </a>
      <Bubble text={text} bare />
    </div>
  );
}

export function Bubble({ text, bare = false }: { text: string; bare?: boolean }) {
  return (
    <div className={bare ? "bubble-caption" : "bubble"} aria-label="WhatsApp message preview">
      {text.split("\n").map((line, i) => (
        <div key={i} className="bubble-line">
          {line === ""
            ? " "
            : whatsappSegments(line).map((seg, j) =>
                seg.bold ? <strong key={j}>{seg.text}</strong> : seg.italic ? <em key={j}>{seg.text}</em> : <span key={j}>{seg.text}</span>,
              )}
        </div>
      ))}
    </div>
  );
}
