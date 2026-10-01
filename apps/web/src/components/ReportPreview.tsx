import { useState } from "react";

import { whatsappSegments } from "../logic";
import { Modal } from "./Modal";

/** The message as WhatsApp will show it: the table image (if any) with the
 * text as its caption, or just the text. Click the image to see it large. */
export function Report({ text, imageUrl }: { text: string; imageUrl: string | null }) {
  const [large, setLarge] = useState(false);
  if (!imageUrl) return <Bubble text={text} />;
  return (
    <div className="bubble bubble-image">
      <button className="image-button" onClick={() => setLarge(true)} title="Click to enlarge">
        <img src={imageUrl} alt="Report table that will be sent" className="report-image" />
      </button>
      <Bubble text={text} bare />
      {large && (
        <Modal title="Report table" onClose={() => setLarge(false)} wide>
          <img src={imageUrl} alt="Report table, full size" className="report-image report-image-large" />
        </Modal>
      )}
    </div>
  );
}

export function Bubble({ text, bare = false }: { text: string; bare?: boolean }) {
  return (
    <div className={bare ? "bubble-caption" : "bubble"} aria-label="WhatsApp message preview">
      {text.split("\n").map((line, i) => (
        <div key={i} className="bubble-line">
          {line === ""
            ? " "
            : whatsappSegments(line).map((seg, j) =>
                seg.bold ? <strong key={j}>{seg.text}</strong> : seg.italic ? <em key={j}>{seg.text}</em> : <span key={j}>{seg.text}</span>,
              )}
        </div>
      ))}
    </div>
  );
}
