"""Viewport, zoom (up to 1000%), panning, and resolution-capped cropping for images. UI-free."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image as PILImage

ZOOMS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0)
FIT = ZOOMS.index(1.0)
DRAFT_MAX_PX = 480  # lower resolution while dragging so panning stays fluid
CRISP_MAX_PX = 1600  # ceiling for settled crops


def fit_cells(
    img_w: int,
    img_h: int,
    vp_w: int,
    vp_h: int,
    cell_w: int = 10,
    cell_h: int = 20,
) -> tuple[int, int]:
    """Cell dimensions (width, height) that fill `(vp_w, vp_h)` while keeping aspect ratio.

    Both small and large images scale to touch at least one edge of the viewport."""
    img_w, img_h = max(1, img_w), max(1, img_h)
    vp_w, vp_h = max(1, vp_w), max(1, vp_h)
    cell_w, cell_h = max(1, cell_w), max(1, cell_h)
    scale = min((vp_w * cell_w) / img_w, (vp_h * cell_h) / img_h)
    fit_w = max(1, min(vp_w, round(img_w * scale / cell_w)))
    fit_h = max(1, min(vp_h, round(img_h * scale / cell_h)))
    return fit_w, fit_h


@dataclass(frozen=True)
class CropPlan:
    """How a source image maps onto the visible viewport cells."""

    disp_w: int  # widget width in terminal cells (never wider than the viewport)
    disp_h: int  # widget height in terminal cells (never taller than the viewport)
    box: tuple[int, int, int, int]  # (x0, y0, x1, y1) crop box in source pixels
    is_full: bool  # True when the whole source image is visible


@dataclass
class ImageViewport:
    """Zoom level and normalized pan position `(pan_x, pan_y)` in `[0.0, 1.0]`."""

    zoom_index: int = FIT
    pan_x: float = 0.5
    pan_y: float = 0.5

    @property
    def zoom(self) -> float:
        return ZOOMS[self.zoom_index]

    @property
    def is_panned(self) -> bool:
        return self.zoom > 1.0 and (abs(self.pan_x - 0.5) > 1e-6 or abs(self.pan_y - 0.5) > 1e-6)

    def copy(self) -> "ImageViewport":
        return ImageViewport(self.zoom_index, self.pan_x, self.pan_y)

    def step_zoom(self, delta: int) -> bool:
        new = max(0, min(len(ZOOMS) - 1, self.zoom_index + delta))
        if new == self.zoom_index:
            return False
        self.zoom_index = new
        if self.zoom <= 1.0:
            self.pan_x = self.pan_y = 0.5
        return True

    def reset(self) -> bool:
        changed = self.zoom_index != FIT or self.pan_x != 0.5 or self.pan_y != 0.5
        self.zoom_index = FIT
        self.pan_x = self.pan_y = 0.5
        return changed

    def pan_by_cells(
        self,
        dx_cells: float,
        dy_cells: float,
        img_w: int,
        img_h: int,
        vp_w: int,
        vp_h: int,
        cell_w: int = 10,
        cell_h: int = 20,
    ) -> bool:
        """Drag by `(dx_cells, dy_cells)`: dragging right pulls the image right (pan_x -> 0),
        dragging up/left moves the view toward the bottom-right corner (pan -> 1)."""
        if self.zoom <= 1.0:
            return False
        fw, fh = fit_cells(img_w, img_h, vp_w, vp_h, cell_w, cell_h)
        overflow_x = max(0.0, fw * self.zoom - max(1, vp_w))
        overflow_y = max(0.0, fh * self.zoom - max(1, vp_h))
        new_x = max(0.0, min(1.0, self.pan_x - dx_cells / overflow_x)) if overflow_x > 0 else 0.5
        new_y = max(0.0, min(1.0, self.pan_y - dy_cells / overflow_y)) if overflow_y > 0 else 0.5
        changed = abs(new_x - self.pan_x) > 1e-6 or abs(new_y - self.pan_y) > 1e-6
        self.pan_x, self.pan_y = new_x, new_y
        return changed

    def pan_by_fraction(self, dx_frac: float, dy_frac: float) -> bool:
        """Keyboard pan by viewport-overflow fractions."""
        if self.zoom <= 1.0:
            return False
        new_x = max(0.0, min(1.0, self.pan_x + dx_frac))
        new_y = max(0.0, min(1.0, self.pan_y + dy_frac))
        changed = abs(new_x - self.pan_x) > 1e-6 or abs(new_y - self.pan_y) > 1e-6
        self.pan_x, self.pan_y = new_x, new_y
        return changed

    def plan(
        self,
        img_w: int,
        img_h: int,
        vp_w: int,
        vp_h: int,
        cell_w: int = 10,
        cell_h: int = 20,
    ) -> CropPlan:
        """Compute the widget cell size and source pixel crop box for the viewport."""
        img_w, img_h = max(1, img_w), max(1, img_h)
        vp_w, vp_h = max(1, vp_w), max(1, vp_h)
        fw, fh = fit_cells(img_w, img_h, vp_w, vp_h, cell_w, cell_h)
        zw, zh = fw * self.zoom, fh * self.zoom
        disp_w = max(1, min(vp_w, round(zw)))
        disp_h = max(1, min(vp_h, round(zh)))
        frac_w = min(1.0, disp_w / zw) if zw > 0 else 1.0
        frac_h = min(1.0, disp_h / zh) if zh > 0 else 1.0
        crop_w = max(1, min(img_w, round(img_w * frac_w)))
        crop_h = max(1, min(img_h, round(img_h * frac_h)))
        x0 = max(0, min(img_w - crop_w, round((img_w - crop_w) * self.pan_x)))
        y0 = max(0, min(img_h - crop_h, round((img_h - crop_h) * self.pan_y)))
        box = (x0, y0, x0 + crop_w, y0 + crop_h)
        return CropPlan(disp_w, disp_h, box, box == (0, 0, img_w, img_h))


def render_crop(
    source: "PILImage.Image",
    plan: CropPlan,
    cell_w: int = 10,
    cell_h: int = 20,
    draft: bool = False,
) -> "PILImage.Image":
    """Extract `plan.box` and size it for terminal rendering (lower resolution when `draft`)."""
    from PIL import Image

    region = source if plan.is_full else source.crop(plan.box)
    tw = max(1, plan.disp_w * max(1, cell_w))
    th = max(1, plan.disp_h * max(1, cell_h))
    cap = DRAFT_MAX_PX if draft else CRISP_MAX_PX
    if max(tw, th) > cap:
        scale = cap / max(tw, th)
        tw = max(1, round(tw * scale))
        th = max(1, round(th * scale))
    rw, rh = region.size
    upscaling = tw > rw or th > rh
    resample = Image.Resampling.NEAREST if (draft or upscaling) else Image.Resampling.BILINEAR
    if (rw, rh) == (tw, th):
        return region if region is not source else region.copy()
    return region.resize((tw, th), resample)
