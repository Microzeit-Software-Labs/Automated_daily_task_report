"""Draws a ``ReportTable`` as a plain, sheet-style PNG with Pillow.

White background, a grey header row, thin grid lines, black text -- the look
of a printed spreadsheet, no colour coding. Free-text columns (Task, Note)
share the leftover width and word-wrap; the rest size to their content.

Rendered once, at approval, and stored with the snapshot, so the bytes that
are sent are the bytes that were previewed -- nothing here runs at send time.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from interlock.domain.sharing.table import ReportTable

Font = ImageFont.FreeTypeFont | ImageFont.ImageFont

_FONT_DIRS = (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"))
_REGULAR = ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")
_BOLD = ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf")

WIDTH = 1600
MARGIN = 48
PAD_X = 16
PAD_Y = 12
LINE_GAP = 6

INK = (17, 17, 17)
MUTED = (96, 96, 96)
GRID = (191, 191, 191)
HEADER_FILL = (242, 242, 242)
WHITE = (255, 255, 255)


def _font(names: tuple[str, ...], size: int) -> Font:
    for directory in _FONT_DIRS:
        for name in names:
            path = directory / name
            if path.is_file():
                return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


class PillowTableRenderer:
    def __init__(self, *, body_size: int = 26) -> None:
        self._body = _font(_REGULAR, body_size)
        self._bold = _font(_BOLD, body_size)
        self._title = _font(_BOLD, round(body_size * 1.55))
        self._small = _font(_REGULAR, round(body_size * 0.85))
        self._line_h = self._height(self._body) + LINE_GAP

    def render(self, table: ReportTable) -> bytes:
        measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        widths = self._column_widths(measure, table)

        header_lines = [
            self._wrap(measure, c, self._bold, w - 2 * PAD_X)
            for c, w in zip(table.columns, widths, strict=True)
        ]
        body_lines = [
            [
                self._wrap(measure, cell, self._body, w - 2 * PAD_X)
                for cell, w in zip(row, widths, strict=True)
            ]
            for row in table.rows
        ]
        header_h = self._row_height(header_lines)
        row_heights = [self._row_height(cells) for cells in body_lines]

        title_h = self._height(self._title)
        small_h = self._height(self._small)
        table_top = MARGIN + title_h + 10 + small_h + 28
        table_h = header_h + sum(row_heights)
        footer_top = table_top + table_h + 22
        height = footer_top + small_h * 2 + MARGIN

        image = Image.new("RGB", (WIDTH, height), WHITE)
        draw = ImageDraw.Draw(image)
        draw.text((MARGIN, MARGIN), table.title, font=self._title, fill=INK)
        draw.text((MARGIN, MARGIN + title_h + 10), table.subtitle, font=self._small, fill=MUTED)

        left = MARGIN
        right = left + sum(widths)
        draw.rectangle((left, table_top, right, table_top + header_h), fill=HEADER_FILL)
        self._draw_row(draw, left, table_top, widths, header_lines, self._bold)
        y = table_top + header_h
        for cells, row_h in zip(body_lines, row_heights, strict=True):
            self._draw_row(draw, left, y, widths, cells, self._body)
            y += row_h

        # Grid: horizontal rules at every row edge, verticals at every column edge.
        edges_y = [table_top, table_top + header_h]
        for row_h in row_heights:
            edges_y.append(edges_y[-1] + row_h)
        for edge in edges_y:
            draw.line((left, edge, right, edge), fill=GRID, width=2)
        x = left
        for w in (0, *widths):
            x += w
            draw.line((x, table_top, x, edges_y[-1]), fill=GRID, width=2)

        if not table.rows:
            draw.text(
                (left + PAD_X, edges_y[-1] + 8), "No open tasks.", font=self._body, fill=MUTED
            )
        draw.text((MARGIN, footer_top), table.footer, font=self._small, fill=MUTED)

        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()

    # -- layout -------------------------------------------------------------

    def _column_widths(self, measure: ImageDraw.ImageDraw, table: ReportTable) -> list[int]:
        """Narrow columns fit their longest cell; wide ones share the rest."""
        available = WIDTH - 2 * MARGIN
        widths = [0] * len(table.columns)
        for i, name in enumerate(table.columns):
            if i in table.wide_columns:
                continue
            longest = max(
                [measure.textlength(name, font=self._bold)]
                + [measure.textlength(row[i], font=self._body) for row in table.rows]
            )
            widths[i] = int(longest) + 2 * PAD_X + 2
        leftover = max(available - sum(widths), 200 * max(len(table.wide_columns), 1))
        wide = sorted(table.wide_columns)
        # Split in proportion to how much text each wide column really holds,
        # so a short Task column doesn't squeeze long notes -- but never below
        # 30% each, so neither collapses.
        natural = [
            max([measure.textlength(r[i], font=self._body) for r in table.rows] + [1.0])
            for i in wide
        ]
        total = sum(natural)
        shares = [n / total for n in natural]
        if len(shares) == 2:
            first = min(max(shares[0], 0.3), 0.7)
            shares = [first, 1 - first]
        for index, share in zip(wide, shares, strict=True):
            widths[index] = int(leftover * share)
        return widths

    def _wrap(self, measure: ImageDraw.ImageDraw, text: str, font: Font, max_w: float) -> list[str]:
        lines: list[str] = []
        for paragraph in text.splitlines() or [""]:
            current = ""
            for word in paragraph.split(" "):
                candidate = f"{current} {word}".strip()
                if measure.textlength(candidate, font=font) <= max_w:
                    current = candidate
                    continue
                if current:
                    lines.append(current)
                # A single word longer than the cell is broken by character.
                while measure.textlength(word, font=font) > max_w and len(word) > 1:
                    cut = len(word)
                    while cut > 1 and measure.textlength(word[:cut], font=font) > max_w:
                        cut -= 1
                    lines.append(word[:cut])
                    word = word[cut:]
                current = word
            lines.append(current)
        return lines

    def _row_height(self, cells: list[list[str]]) -> int:
        return max(len(lines) for lines in cells) * self._line_h + 2 * PAD_Y

    def _draw_row(
        self,
        draw: ImageDraw.ImageDraw,
        left: int,
        top: int,
        widths: list[int],
        cells: list[list[str]],
        font: Font,
    ) -> None:
        x = left
        for lines, w in zip(cells, widths, strict=True):
            for n, line in enumerate(lines):
                draw.text((x + PAD_X, top + PAD_Y + n * self._line_h), line, font=font, fill=INK)
            x += w

    @staticmethod
    def _height(font: Font) -> int:
        box = font.getbbox("Hg")
        return int(box[3] - box[1]) + 4
