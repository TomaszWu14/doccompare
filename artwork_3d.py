"""
artwork_3d.py — generowanie modelu 3D opakowania (GLB) z artworku PDF (dieline).

Pipeline:
  1. extract_dieline_info()  — wymiary W×H×D [mm] z tabelki artworku (regex na tekście),
                               rozmiar strony, typ opakowania.
  2. locate_panels()         — heurystyka wymiarowa: linie cięcia/bigowania z
                               page.get_drawings() → klastry współrzędnych → komórki siatki
                               dopasowane do wymiarów paneli (front/back/left/right/top/bottom).
  3. render_panels()         — render wycinków strony (clip) do PIL.Image w zadanym DPI.
  4. build_glb()             — prostopadłościan o rzeczywistych proporcjach, 6 ścian
                               z teksturami UV, eksport do samodzielnego pliku GLB (trimesh).

Konwencje:
  - Wymiary w mm: W (szerokość, oś X), H (wysokość, oś Y), D (głębokość, oś Z).
  - Dieline ACME są w skali 1:1 — współrzędne PDF pt → mm (× 25.4/72).
  - glTF używa metrów — build_glb() dzieli mm przez 1000.
  - Ciężkie importy (pymupdf, PIL, trimesh, numpy) są lazy — moduł importuje się bez nich.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PT_TO_MM = 25.4 / 72.0
MM_TO_PT = 72.0 / 25.4

# Nazwy paneli i ich wymiary docelowe (szer_panelu, wys_panelu) jako funkcja (W, H, D)
PANEL_TARGETS = {
    "front":  lambda w, h, d: (w, h),
    "back":   lambda w, h, d: (w, h),
    "left":   lambda w, h, d: (d, h),
    "right":  lambda w, h, d: (d, h),
    "top":    lambda w, h, d: (w, d),
    "bottom": lambda w, h, d: (w, d),
}

# Rotacja tekstury panelu w stopniach (CCW) przed nałożeniem na ścianę.
# W dielinach klapowych panele góra/dół często leżą "do góry nogami" względem frontu.
PANEL_ROTATION = {"front": 0, "back": 0, "left": 0, "right": 0, "top": 0, "bottom": 180}

# Fallback: gdy panelu brak w siatce, użyj tekstury panelu lustrzanego
PANEL_MIRROR = {"back": "front", "right": "left", "bottom": "top",
                "front": "back", "left": "right", "top": "bottom"}

MAX_TEXTURE_PX = 2048   # dłuższy bok tekstury ściany
DEFAULT_DPI = 220
MAX_PANEL_PIXELS = 40_000_000  # bezpiecznik na pojedynczy render clip


@dataclass
class DielineInfo:
    w_mm: float = 0.0
    h_mm: float = 0.0
    d_mm: float = 0.0
    page_w_mm: float = 0.0
    page_h_mm: float = 0.0
    page_index: int = 0
    package_type: str = ""      # indywidualne / pośrednie / karton transportowy
    finish: str = ""            # np. "matt foil" — wpływa na roughness materiału
    ean: str = ""
    warnings: list = field(default_factory=list)

    @property
    def has_dims(self) -> bool:
        return self.w_mm > 0 and self.h_mm > 0 and self.d_mm > 0


# ───────────────────────────── 1. Parsowanie wymiarów ─────────────────────────────

_DIMS_RE = re.compile(
    r"(\d{1,4}(?:[.,]\d+)?)\s*[x×]\s*(\d{1,4}(?:[.,]\d+)?)\s*[x×]\s*(\d{1,4}(?:[.,]\d+)?)"
    r"\s*(?:mm)?",
    re.IGNORECASE,
)

_EAN_RE = re.compile(r"\b(\d{13})\b")

_PKG_TYPES = ("karton transportowy", "pośrednie", "posrednie", "indywidualne")


def parse_dimensions_mm(text: str):
    """Znajdź 'W x H x D [mm]' w tekście artworku. Zwraca (w, h, d) w mm lub None.

    Bierze pierwszy trójwymiarowy zapis, w którym wszystkie wartości mieszczą się
    w sensownym zakresie opakowania (5–2000 mm).
    """
    if not text:
        return None
    for m in _DIMS_RE.finditer(text):
        try:
            vals = [float(v.replace(",", ".")) for v in m.groups()]
        except ValueError:
            continue
        if all(5.0 <= v <= 2000.0 for v in vals):
            return tuple(vals)
    return None


def detect_package_type(text: str) -> str:
    """Typ opakowania z tabelki — wybiera pozycję oznaczoną 'x'/'■' jeśli się da,
    inaczej pierwszy występujący typ."""
    low = (text or "").lower()
    for t in _PKG_TYPES:
        if t in low:
            return "pośrednie" if t == "posrednie" else t
    return ""


def extract_dieline_info(pdf_path: str, page_index=None) -> DielineInfo:
    """Odczytaj wymiary i metadane z artworku. Wybiera stronę o największej powierzchni,
    chyba że page_index podany jawnie."""
    import pymupdf

    info = DielineInfo()
    with pymupdf.open(pdf_path) as doc:
        if page_index is None:
            page_index = max(range(len(doc)),
                             key=lambda i: doc[i].rect.width * doc[i].rect.height)
        page = doc[page_index]
        info.page_index = page_index
        info.page_w_mm = page.rect.width * PT_TO_MM
        info.page_h_mm = page.rect.height * PT_TO_MM
        text = page.get_text() or ""

    dims = parse_dimensions_mm(text)
    if dims:
        info.w_mm, info.h_mm, info.d_mm = dims
    else:
        info.warnings.append("Nie znaleziono wymiarów W×H×D w tekście artworku — podaj ręcznie.")
    info.package_type = detect_package_type(text)
    if "foil" in text.lower():
        info.finish = "matt foil"
    ean = _EAN_RE.search(text)
    if ean:
        info.ean = ean.group(1)
    return info


# ───────────────────────────── 2. Lokalizacja paneli ─────────────────────────────

def cluster_coords(values, tol: float):
    """Klastruj posortowane współrzędne 1D: wartości bliższe niż tol → jeden klaster.
    Zwraca listę środków klastrów (posortowaną)."""
    if not values:
        return []
    vals = sorted(values)
    clusters = [[vals[0]]]
    for v in vals[1:]:
        if v - clusters[-1][-1] <= tol:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [sum(c) / len(c) for c in clusters]


def collect_line_coords(page, min_len_mm: float):
    """Z page.get_drawings() zbierz współrzędne [mm] długich segmentów pionowych (x)
    i poziomych (y). Krótkie segmenty (grafika) są odfiltrowane."""
    xs, ys = [], []
    try:
        drawings = page.get_drawings()
    except Exception as e:  # uszkodzony content stream nie powinien wywracać całości
        logger.warning("get_drawings failed: %s", e)
        return xs, ys
    for d in drawings:
        for item in d.get("items", []):
            op = item[0]
            if op == "l":  # linia
                p1, p2 = item[1], item[2]
                segs = [(p1, p2)]
            elif op == "re":  # prostokąt → 4 krawędzie
                r = item[1]
                segs = [(r.top_left, r.top_right), (r.bottom_left, r.bottom_right),
                        (r.top_left, r.bottom_left), (r.top_right, r.bottom_right)]
            else:
                continue
            for p1, p2 in segs:
                dx = abs(p2.x - p1.x) * PT_TO_MM
                dy = abs(p2.y - p1.y) * PT_TO_MM
                if dx <= 1.0 and dy >= min_len_mm:      # pionowa
                    xs.append((p1.x + p2.x) / 2 * PT_TO_MM)
                elif dy <= 1.0 and dx >= min_len_mm:    # pozioma
                    ys.append((p1.y + p2.y) / 2 * PT_TO_MM)
    return xs, ys


def _find_cells(xs, ys, tw: float, th: float, tol: float):
    """Wszystkie komórki (x0,y0,x1,y1) o wymiarach ≈ (tw × th) rozpięte na klastrach linii."""
    cells = []
    for i, x0 in enumerate(xs):
        for x1 in xs[i + 1:]:
            if abs((x1 - x0) - tw) > tol:
                continue
            for j, y0 in enumerate(ys):
                for y1 in ys[j + 1:]:
                    if abs((y1 - y0) - th) > tol:
                        continue
                    cells.append((x0, y0, x1, y1))
    return cells


def _overlap(a, b) -> float:
    """Pole części wspólnej dwóch rectów (mm²)."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def match_panels(xs, ys, w: float, h: float, d: float, tol: float = None):
    """Dopasuj panele do komórek siatki. xs/ys — klastry współrzędnych linii [mm].

    Zwraca dict {panel: (x0,y0,x1,y1) w mm} — tylko znalezione panele.
    Pary paneli tego samego rozmiaru (front/back, left/right, top/bottom) dostają
    rozłączne komórki; front = komórka najbliższa środkowi siatki.
    """
    if tol is None:
        tol = max(3.0, 0.02 * max(w, h, d))
    found: dict = {}
    used: list = []

    cx = (min(xs) + max(xs)) / 2 if xs else 0.0
    cy = (min(ys) + max(ys)) / 2 if ys else 0.0

    def center_dist(c):
        return abs((c[0] + c[2]) / 2 - cx) + abs((c[1] + c[3]) / 2 - cy)

    for primary, secondary, (tw, th) in (
        ("front", "back", (w, h)),
        ("left", "right", (d, h)),
        ("top", "bottom", (w, d)),
    ):
        cells = _find_cells(xs, ys, tw, th, tol)
        # odrzuć komórki nachodzące na już przydzielone panele
        cells = [c for c in cells
                 if all(_overlap(c, u) < 0.25 * tw * th for u in used)]
        cells.sort(key=center_dist)
        picked = []
        for c in cells:
            if all(_overlap(c, p) < 0.25 * tw * th for p in picked):
                picked.append(c)
            if len(picked) == 2:
                break
        if picked:
            found[primary] = picked[0]
            used.append(picked[0])
        if len(picked) > 1:
            found[secondary] = picked[1]
            used.append(picked[1])
    return found


def locate_panels(page, info: DielineInfo):
    """Zlokalizuj panele na stronie dieline. Zwraca (rects_mm, warnings)."""
    warnings = []
    w, h, d = info.w_mm, info.h_mm, info.d_mm
    min_len = 0.5 * min(w, h, d)
    xs_raw, ys_raw = collect_line_coords(page, min_len_mm=min_len)
    tol = max(3.0, 0.02 * max(w, h, d))
    xs = cluster_coords(xs_raw, tol=min(tol, 2.5))
    ys = cluster_coords(ys_raw, tol=min(tol, 2.5))

    # sanity: siatka 1:1 musi się mieścić na stronie
    if info.page_w_mm and (w > info.page_w_mm and h > info.page_h_mm):
        warnings.append("Strona mniejsza niż wymiary opakowania — dieline nie jest w skali 1:1.")

    rects = match_panels(xs, ys, w, h, d, tol=tol) if (xs and ys) else {}
    if not rects:
        warnings.append("Nie udało się dopasować paneli do siatki — użyto całej strony jako frontu.")
    else:
        missing = [p for p in PANEL_TARGETS if p not in rects]
        if missing:
            warnings.append("Panele uzupełnione lustrzanie/kolorem tła: " + ", ".join(missing))
    return rects, warnings


# ───────────────────────────── 3. Render paneli ─────────────────────────────

def render_panels(pdf_path: str, page_index: int, rects_mm: dict, dpi: int = DEFAULT_DPI):
    """Renderuj wycinki strony (clip) do dict {panel: PIL.Image (RGB)}."""
    import pymupdf
    from PIL import Image

    panels = {}
    with pymupdf.open(pdf_path) as doc:
        page = doc[page_index]
        for name, (x0, y0, x1, y1) in rects_mm.items():
            clip = pymupdf.Rect(x0 * MM_TO_PT, y0 * MM_TO_PT, x1 * MM_TO_PT, y1 * MM_TO_PT)
            use_dpi = dpi
            # bezpiecznik na megapiksele
            while use_dpi > 40:
                px = (clip.width / 72 * use_dpi) * (clip.height / 72 * use_dpi)
                if px <= MAX_PANEL_PIXELS:
                    break
                use_dpi = int(use_dpi * 0.7)
            pix = page.get_pixmap(clip=clip, dpi=use_dpi, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            panels[name] = img
    return panels


def orient_panel(img, name: str):
    """Obróć teksturę panelu zgodnie z PANEL_ROTATION i ogranicz rozmiar."""
    from PIL import Image

    rot = PANEL_ROTATION.get(name, 0) % 360
    if rot:
        img = img.rotate(rot, expand=True)
    longest = max(img.size)
    if longest > MAX_TEXTURE_PX:
        scale = MAX_TEXTURE_PX / longest
        img = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))), Image.LANCZOS)
    return img


def _corner_color(img):
    """Kolor tła panelu — próbka z rogu (uśredniona 8×8 px)."""
    patch = img.crop((0, 0, min(8, img.width), min(8, img.height))).resize((1, 1))
    return patch.getpixel((0, 0))


def _blank_panel(size_mm, color=(240, 240, 240)):
    from PIL import Image
    w = max(2, min(MAX_TEXTURE_PX, int(size_mm[0] * 4)))
    h = max(2, min(MAX_TEXTURE_PX, int(size_mm[1] * 4)))
    return Image.new("RGB", (w, h), color)


def complete_panels(panels: dict, info: DielineInfo) -> dict:
    """Uzupełnij brakujące panele: lustrzany odpowiednik → kolor tła frontu → jasnoszary."""
    fallback_color = _corner_color(panels["front"]) if "front" in panels else (240, 240, 240)
    out = dict(panels)
    for name in PANEL_TARGETS:
        if name in out:
            continue
        mirror = PANEL_MIRROR.get(name)
        if mirror and mirror in out:
            out[name] = out[mirror].copy()
        else:
            tw, th = PANEL_TARGETS[name](info.w_mm, info.h_mm, info.d_mm)
            out[name] = _blank_panel((tw, th), fallback_color)
    return out


def colorfulness(img) -> float:
    """Miara barwności obrazu (metryka Haslera-Süsstrunka, uproszczona).
    Panel brandowany (logo, kolory Pantone) >> panel z blokiem tekstu prawnego."""
    import numpy as np
    a = np.asarray(img.convert("RGB").resize((64, 64)), dtype=np.float32)
    rg = a[..., 0] - a[..., 1]
    yb = 0.5 * (a[..., 0] + a[..., 1]) - a[..., 2]
    return float((rg.std() ** 2 + yb.std() ** 2) ** 0.5
                 + 0.3 * ((rg.mean() ** 2 + yb.mean() ** 2) ** 0.5))


def pick_front_by_color(panels: dict) -> dict:
    """Jeśli 'back' jest wyraźnie barwniejszy niż 'front' (heurystyka środka siatki
    wskazała panel z tekstem), zamień je miejscami — front ma być panelem brandowanym."""
    out = dict(panels)
    if "front" in out and "back" in out:
        if colorfulness(out["back"]) > 1.2 * colorfulness(out["front"]):
            out["front"], out["back"] = out["back"], out["front"]
    return out


# ───────────────────────────── 4. Budowa GLB ─────────────────────────────

def _face_quads(w: float, h: float, d: float):
    """Definicje 6 ścian: origin + wektory u,v (normalna = u×v skierowana na zewnątrz).
    Wymiary w metrach. UV: (0,0) = lewy-dolny róg tekstury (konwencja trimesh/OBJ)."""
    return {
        "front":  ((-w / 2, -h / 2, +d / 2), (w, 0, 0), (0, h, 0)),
        "back":   ((+w / 2, -h / 2, -d / 2), (-w, 0, 0), (0, h, 0)),
        "left":   ((-w / 2, -h / 2, -d / 2), (0, 0, d), (0, h, 0)),
        "right":  ((+w / 2, -h / 2, +d / 2), (0, 0, -d), (0, h, 0)),
        "top":    ((-w / 2, +h / 2, +d / 2), (w, 0, 0), (0, 0, -d)),
        "bottom": ((-w / 2, -h / 2, -d / 2), (w, 0, 0), (0, 0, d)),
    }


def build_glb(panels: dict, info: DielineInfo, out_path: str) -> str:
    """Zbuduj GLB: prostopadłościan W×H×D z teksturami paneli na 6 ścianach."""
    import numpy as np
    import trimesh
    from trimesh.visual.material import PBRMaterial
    from trimesh.visual.texture import TextureVisuals

    w = info.w_mm / 1000.0
    h = info.h_mm / 1000.0
    d = info.d_mm / 1000.0
    scene = trimesh.Scene()
    metallic = 0.0
    roughness = 0.55 if "foil" in (info.finish or "").lower() else 0.85

    for name, (origin, u, v) in _face_quads(w, h, d).items():
        o = np.array(origin, dtype=np.float64)
        uvec = np.array(u, dtype=np.float64)
        vvec = np.array(v, dtype=np.float64)
        vertices = np.array([o, o + uvec, o + uvec + vvec, o + vvec])
        faces = np.array([[0, 1, 2], [0, 2, 3]])
        uv = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
        material = PBRMaterial(baseColorTexture=panels[name],
                               metallicFactor=metallic, roughnessFactor=roughness,
                               name=f"panel_{name}")
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces,
                               visual=TextureVisuals(uv=uv, material=material),
                               process=False)
        scene.add_geometry(mesh, node_name=name, geom_name=name)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    scene.export(out_path)
    return out_path


# ───────────────────────────── Orkiestracja ─────────────────────────────

def generate_artwork_3d(pdf_path: str, out_path: str, dims_override=None,
                        dpi: int = DEFAULT_DPI) -> dict:
    """Pełny pipeline: PDF dieline → GLB. Zwraca metadane generacji.

    dims_override: (w_mm, h_mm, d_mm) — wymiary podane ręcznie (nadpisują odczytane).
    """
    import pymupdf

    info = extract_dieline_info(pdf_path)
    if dims_override:
        info.w_mm, info.h_mm, info.d_mm = (float(x) for x in dims_override)
        info.warnings = [wrn for wrn in info.warnings if "wymiar" not in wrn.lower()]
    if not info.has_dims:
        raise ValueError("Brak wymiarów opakowania — nie znaleziono ich w PDF i nie podano ręcznie.")

    with pymupdf.open(pdf_path) as doc:
        page = doc[info.page_index]
        rects, loc_warnings = locate_panels(page, info)
    info.warnings.extend(loc_warnings)

    if not rects:
        # fallback: cała strona jako front
        rects = {"front": (0.0, 0.0, info.page_w_mm, info.page_h_mm)}

    raw = render_panels(pdf_path, info.page_index, rects, dpi=dpi)
    panels = {name: orient_panel(img, name) for name, img in raw.items()}
    panels = pick_front_by_color(panels)
    panels = complete_panels(panels, info)
    build_glb(panels, info, out_path)

    return {
        "glb_path": out_path,
        "dims_mm": [info.w_mm, info.h_mm, info.d_mm],
        "package_type": info.package_type,
        "ean": info.ean,
        "panels_found": sorted(rects.keys()),
        "warnings": info.warnings,
    }
