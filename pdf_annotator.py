"""
pdf_annotator.py — renderuje strony PDF do obrazów PNG z kolorowymi adnotacjami.
Kolory: error=czerwony, warning=pomarańczowy, format=fioletowy, info=niebieski, ok=zielony, I_vs_1=intensywny czerwony
"""
import io, re, base64
import pdfplumber
from PIL import Image, ImageDraw

FILL = {
    "error":   (247, 79,  79,  90),
    "warning": (247, 168, 79,  85),
    "info":    (123, 158, 247, 75),
    "ok":      (79,  206, 142, 65),
    "format":  (164, 123, 251, 70),
    "I_vs_1":  (247, 50,  50,  110),
}
BORDER = {
    "error":   (220, 50,  50,  220),
    "warning": (220, 140, 50,  220),
    "info":    (80,  120, 220, 200),
    "ok":      (50,  180, 100, 200),
    "format":  (130, 90,  220, 200),
    "I_vs_1":  (200, 0,   0,   255),
}
LEGEND = [
    {"type": "error",   "label": "Błąd krytyczny",         "color": "#f74f4f"},
    {"type": "I_vs_1",  "label": "Zamiana I↔1",            "color": "#dd1111"},
    {"type": "warning", "label": "Ostrzeżenie / literówka","color": "#f7a84f"},
    {"type": "format",  "label": "Różnica formatu",        "color": "#a47bfb"},
    {"type": "info",    "label": "Uwaga",                  "color": "#7b9ef7"},
    {"type": "ok",      "label": "Pole zgodne",            "color": "#4fce8e"},
]

def _n(s): return re.sub(r"\s+", " ", str(s).lower().strip())

def _find(words, search, fuzzy=True):
    if not search or not words: return []
    sn = _n(search)
    res = []
    # Dokładne
    for w in words:
        if _n(w["text"]) == sn:
            res.append((w["x0"], w["top"], w["x1"], w["bottom"]))
    if res: return res
    # Zawieranie
    if len(sn) >= 4:
        for w in words:
            wn = _n(w["text"])
            # `sn` zawiera się w słowie — OK. Odwrotny kierunek tylko gdy `wn`
            # występuje jako pełny token (granice słów), by uniknąć trafień
            # na krótkie podciągi (np. "s" wewnątrz "at-nfa-s").
            if sn in wn or (
                len(wn) >= 4
                and re.search(r"(?:^|\s)" + re.escape(wn) + r"(?:\s|$)", sn)
            ):
                res.append((w["x0"], w["top"], w["x1"], w["bottom"]))
    if res: return res
    # Fraza
    if " " in sn:
        parts = sn.split(); n = len(parts)
        txts = [_n(w["text"]) for w in words]
        for i in range(len(words)-n+1):
            if txts[i:i+n] == parts:
                res.append((
                    min(words[j]["x0"] for j in range(i,i+n)),
                    min(words[j]["top"] for j in range(i,i+n)),
                    max(words[j]["x1"] for j in range(i,i+n)),
                    max(words[j]["bottom"] for j in range(i,i+n)),
                ))
    if res: return res
    # Fuzzy prefix
    if fuzzy and len(sn) >= 5:
        _ftok = sn.split()  # BUGFIX: sn z samych spacji → [] → IndexError na [0]
        if _ftok:
            tok = _ftok[0][:6]
            for w in words:
                if tok in _n(w["text"])[:10]:
                    res.append((w["x0"], w["top"], w["x1"], w["bottom"]))
                    if len(res) >= 2: break
    return res

def _merge(boxes, gap=4.0):
    if not boxes: return []
    merged = list(boxes); changed = True
    while changed:
        changed = False; out = []; used = set()
        for i, b1 in enumerate(merged):
            if i in used: continue
            x0,t,x1,b = b1
            for j, b2 in enumerate(merged):
                if j<=i or j in used: continue
                if b2[0]<=x1+gap and b2[2]>=x0-gap and b2[1]<=b+gap and b2[3]>=t-gap:
                    x0=min(x0,b2[0]); t=min(t,b2[1]); x1=max(x1,b2[2]); b=max(b,b2[3])
                    used.add(j); changed=True
            out.append((x0,t,x1,b)); used.add(i)
        merged = out
    return merged

def annotate_pdf(pdf_path, annotations, resolution=130, max_pages=3):
    """
    annotations: list of {'texts': list[str], 'type': str, 'label': str}
    Returns: list of {'page':int, 'image_b64':str, 'found':int, 'annotations':list}
    """
    results = []
    with pdfplumber.open(pdf_path) as pdf:
        for pn in range(min(len(pdf.pages), max_pages)):
            page = pdf.pages[pn]
            words = page.extract_words(keep_blank_chars=False, x_tolerance=3, y_tolerance=3)
            if not words: continue
            pil = page.to_image(resolution=resolution).original.convert("RGBA")
            sx = pil.width / page.width; sy = pil.height / page.height
            ov = Image.new("RGBA", pil.size, (0,0,0,0))
            draw = ImageDraw.Draw(ov)
            found = 0; panns = []
            for ann in annotations:
                texts = ann.get("texts", [])
                if isinstance(texts, str): texts = [texts]
                atype = ann.get("type", "error")
                fill = FILL.get(atype, FILL["error"])
                bdr  = BORDER.get(atype, BORDER["error"])
                all_boxes = []
                for t in texts:
                    all_boxes.extend(_find(words, t))
                all_boxes = _merge(all_boxes)
                for (x0,top,x1,bottom) in all_boxes:
                    px0=max(0,int(x0*sx)-3); py0=max(0,int(top*sy)-3)
                    px1=min(pil.width,int(x1*sx)+3); py1=min(pil.height,int(bottom*sy)+3)
                    draw.rectangle([px0,py0,px1,py1], fill=fill)
                    lw = 3 if atype in ("error","I_vs_1") else 2
                    draw.rectangle([px0,py0,px1,py1], outline=bdr, width=lw)
                    found += 1
                    panns.append({"type":atype,"label":ann.get("label",""),"box_px":[px0,py0,px1,py1]})
            combined = Image.alpha_composite(pil, ov).convert("RGB")
            buf = io.BytesIO(); combined.save(buf, format="PNG", optimize=True)
            results.append({
                "page": pn+1,
                "image_b64": base64.b64encode(buf.getvalue()).decode(),
                "found": found,
                "annotations": panns,
                "width_px": pil.width, "height_px": pil.height,
            })
    return results

def results_to_annotations(report, side="a"):
    """Konwertuje unified raport na adnotacje dla dokumentu A lub B."""
    anns = []; mods = report.get("modules", {})
    val_key = f"val_{side}" if side in ("a","b") else f"doc_{side}"
    qa = f"qty_{side}"; na = f"net_{side}"

    tbl = mods.get("table", {})
    if tbl.get("ok"):
        for item in tbl.get("items", []):
            status = item.get("status","ok"); ref = item.get("ref","")
            if not ref: continue
            atype = ("error" if status=="roznica" else
                     "warning" if status in ("watpliwe",f"tylko_{side}") else
                     "format" if status=="format" else "ok")
            texts = [ref]
            if status=="roznica":
                if item.get(qa): texts.append(item[qa])
                if item.get(na): texts.append(item[na])
            anns.append({"texts":texts,"type":atype,
                          "label":"; ".join(item.get("issues",[]) or [status])})
        for h in tbl.get("headers",[]):
            hs = h.get("status","ok"); vk = f"val_{side}"
            if hs in ("roznica", f"brak_w_{'b' if side=='a' else 'a'}"):
                val = h.get(vk,"") or ""
                if val:
                    anns.append({"texts":[val],"type":"error",
                                  "label":f"{h.get('key','')}: {h.get('comment','')}"})

    typo = mods.get("typo",{})
    if typo.get("ok"):
        vk = f"val_{side}"
        for f in typo.get("findings",[]):
            sev=f.get("severity","info"); cat=f.get("category","")
            val = f.get(vk,"") or ""
            if not val: continue
            atype = "I_vs_1" if cat=="I_vs_1" else ("error" if sev=="error" else "warning")
            anns.append({"texts":[val],"type":atype,"label":f.get("description","")[:80]})

    regex = mods.get("regex",{})
    if regex.get("ok"):
        dk = f"doc_{side}"
        for field in regex.get("fields",[]):
            fs=field.get("status","ok")
            if fs in ("roznica","watpliwe",f"brak_w_{'b' if side=='a' else 'a'}"):
                val=field.get(dk,"") or ""
                if val and len(val)>=3:
                    anns.append({"texts":[val],"type":"warning" if fs=="watpliwe" else "error",
                                  "label":field.get("field","")})

    # Deduplikacja
    seen=set(); out=[]
    for a in anns:
        key=(tuple(sorted(str(t) for t in a.get("texts",[]))),a.get("type",""))
        if key not in seen and any(str(t).strip() for t in a.get("texts",[])):
            seen.add(key); out.append(a)
    return out
