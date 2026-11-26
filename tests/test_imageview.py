from PIL import Image

from wandb_cli_tool.imageview import (
    DRAFT_MAX_PX, FIT, ZOOMS, ImageViewport, fit_cells, render_crop,
)


def test_fit_cells_fills_viewport_preserving_aspect_ratio():
    # Small 8x8 square image scales UP to fill height (40 cells @ 10x20px -> 80x40 cells = 800x800px)
    assert fit_cells(8, 8, 120, 40, cell_w=10, cell_h=20) == (80, 40)
    # Wide 1920x1080 image scales DOWN to fill width cleanly
    fw, fh = fit_cells(1920, 1080, 120, 40, cell_w=10, cell_h=20)
    assert fw == 120 and fh == 34
    assert fw <= 120 and fh <= 40


def test_zooms_reach_1000_percent():
    assert ZOOMS[FIT] == 1.0
    assert ZOOMS[-1] == 10.0


def test_plan_crops_to_viewport_and_keeps_widget_inside_viewport():
    vp = ImageViewport()
    p100 = vp.plan(1000, 1000, 120, 40)
    assert p100.is_full and p100.box == (0, 0, 1000, 1000)
    assert (p100.disp_w, p100.disp_h) == (80, 40)

    # Zooming to 1000% keeps widget bounded by viewport (120, 40) and crops ~10% vertically
    vp.zoom_index = len(ZOOMS) - 1
    p_center = vp.plan(1000, 1000, 120, 40)
    assert not p_center.is_full
    assert p_center.disp_w <= 120 and p_center.disp_h <= 40
    assert p_center.box[2] - p_center.box[0] < 200
    assert p_center.box[3] - p_center.box[1] == 100

    # Panning to bottom-left corner puts x0=0, y1=1000
    vp.pan_x, vp.pan_y = 0.0, 1.0
    p_bl = vp.plan(1000, 1000, 120, 40)
    assert p_bl.box[0] == 0 and p_bl.box[3] == 1000

    # Panning to bottom-right corner puts x1=1000, y1=1000
    vp.pan_x, vp.pan_y = 1.0, 1.0
    p_br = vp.plan(1000, 1000, 120, 40)
    assert p_br.box[2] == 1000 and p_br.box[3] == 1000


def test_pan_by_cells_and_fraction_behave_as_grab_and_drag():
    vp = ImageViewport()
    assert vp.pan_by_cells(-10, -5, 1000, 1000, 120, 40) is False  # no pan at 100%
    assert vp.pan_by_fraction(0.2, 0.2) is False
    vp.step_zoom(2)  # 200%
    # Dragging up-left (negative dx/dy) moves viewport toward bottom-right (pan -> 1.0)
    assert vp.pan_by_cells(-10, -5, 1000, 1000, 120, 40) is True
    assert vp.pan_x > 0.5 and vp.pan_y > 0.5
    # Dragging way past edges clamps to [0, 1]
    vp.pan_by_cells(-5000, -5000, 1000, 1000, 120, 40)
    assert (vp.pan_x, vp.pan_y) == (1.0, 1.0)
    vp.pan_by_cells(5000, 5000, 1000, 1000, 120, 40)
    assert (vp.pan_x, vp.pan_y) == (0.0, 0.0)
    # Resetting zoom back to <= 100% re-centers
    while vp.zoom > 1.0:
        vp.step_zoom(-1)
    assert (vp.pan_x, vp.pan_y) == (0.5, 0.5)


def test_render_crop_caps_resolution_when_draft_and_extracts_exact_region():
    # Quadrant image: red top-left, green top-right, blue bottom-left, yellow bottom-right
    src = Image.new("RGB", (200, 200))
    for x in range(200):
        for y in range(200):
            color = (255, 0, 0) if (x < 100 and y < 100) else (0, 255, 0) if y < 100 else (0, 0, 255) if x < 100 else (255, 255, 0)
            src.putpixel((x, y), color)
    vp = ImageViewport(zoom_index=ZOOMS.index(4.0), pan_x=0.0, pan_y=1.0)
    plan = vp.plan(200, 200, 80, 40)
    draft_img = render_crop(src, plan, cell_w=10, cell_h=20, draft=True)
    assert max(draft_img.size) <= DRAFT_MAX_PX
    assert draft_img.getpixel((draft_img.width // 2, draft_img.height // 2)) == (0, 0, 255)  # bottom-left = blue
