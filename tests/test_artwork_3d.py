"""Testy artwork_3d — czysta logika (bez PDF-ów), GLB przez importorskip(trimesh)."""

import os

import pytest

from artwork_3d import (
    DielineInfo,
    PANEL_TARGETS,
    cluster_coords,
    detect_package_type,
    match_panels,
    parse_dimensions_mm,
)


# ───────────── parse_dimensions_mm ─────────────

@pytest.mark.parametrize("text,expected", [
    ("Rozmiar [mm]: 210 x 120 x 55 mm", (210.0, 120.0, 55.0)),
    ("Rozmiar [mm] szer. wys. 290 x 250 x 222", (290.0, 250.0, 222.0)),
    ("wymiary 100×80×40 mm", (100.0, 80.0, 40.0)),
    ("120,5 x 60 x 30", (120.5, 60.0, 30.0)),
    ("bez wymiarow", None),
    ("", None),
    (None, None),
])
def test_parse_dimensions(text, expected):
    assert parse_dimensions_mm(text) == expected


def test_parse_dimensions_skips_out_of_range():
    # 5907 x 99 x 68 (fragment EAN-u) — poza zakresem 5–2000 mm, ma być pominięty
    assert parse_dimensions_mm("kod 5907x99x68, pudelko 210 x 120 x 55 mm") == (210.0, 120.0, 55.0)


def test_detect_package_type():
    assert detect_package_type("x pośrednie | matt foil coated") == "pośrednie"
    assert detect_package_type("KARTON TRANSPORTOWY") == "karton transportowy"
    assert detect_package_type("cokolwiek") == ""


# ───────────── cluster_coords ─────────────

def test_cluster_coords_merges_close_values():
    vals = [0.0, 0.4, 0.9, 55.1, 54.8, 210.0]
    out = cluster_coords(vals, tol=1.0)
    assert len(out) == 3
    assert out[0] == pytest.approx(0.43, abs=0.1)
    assert out[1] == pytest.approx(54.95, abs=0.1)
    assert out[2] == pytest.approx(210.0)


def test_cluster_coords_empty():
    assert cluster_coords([], tol=1.0) == []


# ───────────── match_panels ─────────────

def _wrap_grid(w, h, d):
    """Syntetyczna siatka owijkowa: [left D | front W | right D | back W] × wysokość H,
    z klapami top/bottom (W×D) nad i pod frontem."""
    x0 = 10.0
    xs = [x0, x0 + d, x0 + d + w, x0 + 2 * d + w, x0 + 2 * d + 2 * w]
    y0 = 40.0
    ys = [y0 - d, y0, y0 + h, y0 + h + d]
    return xs, ys


def test_match_panels_full_wrap():
    w, h, d = 210.0, 120.0, 55.0
    xs, ys = _wrap_grid(w, h, d)
    rects = match_panels(xs, ys, w, h, d)
    for panel in ("front", "back", "left", "right", "top", "bottom"):
        assert panel in rects, f"brak panelu {panel}"
    # panele front/back muszą mieć wymiar W×H
    for panel in ("front", "back"):
        x0, y0, x1, y1 = rects[panel]
        assert (x1 - x0) == pytest.approx(w, abs=3.0)
        assert (y1 - y0) == pytest.approx(h, abs=3.0)
    # left/right: D×H
    for panel in ("left", "right"):
        x0, y0, x1, y1 = rects[panel]
        assert (x1 - x0) == pytest.approx(d, abs=3.0)
    # front i back nie nachodzą na siebie
    fa, ba = rects["front"], rects["back"]
    assert fa != ba


def test_match_panels_disjoint_assignment():
    """Pary paneli tego samego rozmiaru dostają rozłączne komórki."""
    w, h, d = 100.0, 80.0, 40.0
    xs, ys = _wrap_grid(w, h, d)
    rects = match_panels(xs, ys, w, h, d)

    def overlap(a, b):
        ow = min(a[2], b[2]) - max(a[0], b[0])
        oh = min(a[3], b[3]) - max(a[1], b[1])
        return max(0, ow) * max(0, oh)

    assert overlap(rects["front"], rects["back"]) == 0
    assert overlap(rects["left"], rects["right"]) == 0


def test_match_panels_empty_grid():
    assert match_panels([], [], 100, 80, 40) == {}


def test_match_panels_partial_grid_returns_subset():
    # tylko dwie linie pionowe w odstępie W i dwie poziome w odstępie H → sam front
    w, h, d = 210.0, 120.0, 55.0
    rects = match_panels([0.0, w], [0.0, h], w, h, d)
    assert "front" in rects
    assert "back" not in rects


# ───────────── complete_panels / orient_panel ─────────────

def test_complete_panels_mirrors_and_blanks():
    PIL = pytest.importorskip("PIL.Image")
    from artwork_3d import complete_panels

    info = DielineInfo(w_mm=210, h_mm=120, d_mm=55)
    front = PIL.new("RGB", (64, 32), (10, 20, 30))
    out = complete_panels({"front": front}, info)
    assert set(out.keys()) == set(PANEL_TARGETS.keys())
    # back = kopia frontu (lustro), top/bottom = kolor tła frontu
    assert out["back"].size == front.size
    assert out["top"].getpixel((0, 0)) == (10, 20, 30)


def test_orient_panel_rotation_and_limit():
    PIL = pytest.importorskip("PIL.Image")
    from artwork_3d import MAX_TEXTURE_PX, orient_panel

    img = PIL.new("RGB", (100, 50), (1, 2, 3))
    rotated = orient_panel(img, "bottom")   # PANEL_ROTATION[bottom] == 180
    assert rotated.size == (100, 50)
    big = PIL.new("RGB", (MAX_TEXTURE_PX * 2, 100), (0, 0, 0))
    small = orient_panel(big, "front")
    assert max(small.size) <= MAX_TEXTURE_PX


def test_pick_front_by_color_swaps_text_panel():
    PIL = pytest.importorskip("PIL.Image")
    from artwork_3d import pick_front_by_color

    grayish = PIL.new("RGB", (64, 64), (245, 245, 245))     # blok tekstu
    branded = PIL.new("RGB", (64, 64), (10, 60, 160))       # panel z kolorem
    # połowa brandowanego żółta — wysoka barwność
    for x in range(32):
        for y in range(64):
            branded.putpixel((x, y), (250, 200, 20))
    out = pick_front_by_color({"front": grayish, "back": branded})
    assert out["front"] is branded
    assert out["back"] is grayish
    # gdy front już jest barwny — bez zmian
    out2 = pick_front_by_color({"front": branded, "back": grayish})
    assert out2["front"] is branded


# ───────────── build_glb ─────────────

def test_build_glb_roundtrip(tmp_path):
    trimesh = pytest.importorskip("trimesh")
    PIL = pytest.importorskip("PIL.Image")
    from artwork_3d import build_glb

    info = DielineInfo(w_mm=210, h_mm=120, d_mm=55)
    panels = {name: PIL.new("RGB", (64, 64), (i * 30, 100, 150))
              for i, name in enumerate(PANEL_TARGETS)}
    out = str(tmp_path / "box.glb")
    build_glb(panels, info, out)

    assert os.path.exists(out)
    with open(out, "rb") as f:
        assert f.read(4) == b"glTF"

    scene = trimesh.load(out)
    bounds = scene.bounds
    dims_m = bounds[1] - bounds[0]
    assert dims_m[0] == pytest.approx(0.210, abs=1e-6)
    assert dims_m[1] == pytest.approx(0.120, abs=1e-6)
    assert dims_m[2] == pytest.approx(0.055, abs=1e-6)
    assert len(scene.geometry) == 6
