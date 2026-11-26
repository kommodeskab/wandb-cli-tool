"""The widget used for "auto" mode, with fixes for textual-image's sixel widget.

textual-image's own sixel widget (0.12.0 in particular) can draw outside its area:
- after the image it leaves the cursor one row BELOW the image; Textual then writes the rest of
  that line (and a newline) there, which blanks part of the bar underneath or, on the last screen
  row, scrolls the whole terminal (the header/key bar appear twice);
- it leaves the rows of the image empty instead of clearing them, so an old image can linger
  next to a new, differently sized one after zooming or panning;
- a sixel is drawn in bands of 6 pixels, so an image whose height is not a multiple of 6 runs a
  few pixels into the row below.
`SafeSixelImage` keeps everything inside the widget's own cells.
"""

from __future__ import annotations

from typing import Iterable

import textual_image.widget as tiw
from rich.control import Control
from rich.segment import ControlType, Segment
from rich.style import Style
from textual.geometry import Region
from textual.strip import Strip
from textual_image.widget.sixel import Image as SixelImage
from textual_image.widget.sixel import _ImageSixelImpl, _NoopRenderable

SIXEL_BAND = 6  # pixel rows per sixel band
_NULL = Style()


class _SafeSixelImpl(_ImageSixelImpl, can_focus=False, inherit_css=False):
    DEFAULT_CSS = "_SafeSixelImpl { width: 100%; height: 100%; }"

    def render_lines(self, crop: Region) -> list[Strip]:
        lines = super().render_lines(crop)
        if not lines:
            return lines
        _, color = self.background_colors
        clear = Segment(" " * crop.width, Style(bgcolor=color.rich_color))
        out = [Strip([clear], cell_length=crop.width) for _ in lines[:-1]]
        sixel = [s for s in lines[-1] if "\x1b" in s.text]  # cursor moves + sixel data
        out.append(Strip([clear, *sixel], cell_length=crop.width))
        return out

    def _image_to_sixels(self, image, sixel_options=None, background=None) -> str:
        height = image.height - image.height % SIXEL_BAND  # never spill into the row below
        if height and height != image.height:
            image = image.crop((0, 0, image.width, height))
        data = super()._image_to_sixels(image, sixel_options, background)
        # Do not advance the cursor another band below the last pixel row.
        return data[:-3] + "\x1b\\" if data.endswith("-\x1b\\") else data

    def _get_sixel_segments(self, sixel_data: str) -> Iterable[Segment]:
        region = self.screen.find_widget(self).visible_region
        ctrl = ((ControlType.CURSOR_FORWARD, 0),)
        return [
            Segment(Control.move_to(region.x, region.y).segment.text, style=_NULL, control=ctrl),
            Segment(sixel_data, style=_NULL, control=ctrl),
            # right of the image on its LAST row, where Textual continues that line
            Segment(Control.move_to(region.right, region.bottom - 1).segment.text, style=_NULL, control=ctrl),
        ]


class SafeSixelImage(SixelImage, Renderable=_NoopRenderable):
    """textual-image's sixel widget, drawing strictly inside its own cells."""

    def compose(self):
        yield _SafeSixelImpl(self.image, self._sixel_options)


# What "auto" means: the dedicated sixel widget on sixel terminals (NEVER AutoImage there, which
# leaves a blank area), otherwise the library's best choice (kitty graphics or character cells).
BEST_IMAGE = SafeSixelImage if tiw.Image is SixelImage else tiw.Image
