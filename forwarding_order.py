"""
forwarding_order.py — czysta logika zlecenia spedycyjnego (KOLEJKA-02, D-02).

Buduje dane dokumentu zlecenia spedycyjnego z już pobranych wierszy
kolejka_zlecenia + kolejka_kontenery + freight_forwarders i renderuje PDF
w pamięci (reportlab). Bramka: tylko POTWIERDZONE zlecenie
(zgoda_wyplyniecie == 1) z przypisanym spedytorem może wygenerować dokument.

Moduł zawiera wyłącznie czyste funkcje — żadnego dostępu do DB ani plików;
wiersze przekazuje wywołujący (blueprints/kolejka.py) jako zwykłe dicty.
"""
from __future__ import annotations

import io

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False


# ── Bramka wymaganych pól (styl lookup + list-diff, jak missing_required_docs) ─

_REQUIRED_CHECKS = [
    # (nazwa pola, predykat "pole obecne/spełnione")
    ("zgoda_wyplyniecie", lambda row: row.get("zgoda_wyplyniecie") == 1),
    ("forwarder_id",      lambda row: bool(row.get("forwarder_id"))),
]


def required_fields(zlecenie_row: dict) -> list:
    """Zwraca listę brakujących pól blokujących generowanie zlecenia
    spedycyjnego (D-02): zgoda_wyplyniecie musi być =1 (potwierdzone),
    spedytor musi być przypisany. Pusta lista = można generować."""
    row = zlecenie_row or {}
    return [name for name, ok in _REQUIRED_CHECKS if not ok(row)]


def build_forwarding_order_data(zlecenie_row: dict, kontener_row: dict | None,
                                forwarder_row: dict | None) -> dict:
    """Składa płaski dict pól dokumentu zlecenia spedycyjnego."""
    z = zlecenie_row or {}
    k = kontener_row or {}
    f = forwarder_row or {}
    return {
        "nr_zamowienia":     z.get("nr_zamowienia") or "",
        "supplier_name":     z.get("supplier_name") or "",
        "rodzaj_transportu": z.get("rodzaj_transportu") or k.get("typ_transportu") or "",
        "planowane_etd":     z.get("planowane_etd") or k.get("etd_plan") or "",
        "data_dostawy":      z.get("data_dostawy") or "",
        "numer_kontenera":   k.get("numer_kontenera") or "",
        "eta":               k.get("eta") or "",
        "origin_port":       k.get("origin_port") or "",
        "dest_port":         k.get("dest_port") or "",
        "forwarder_name":    (f.get("company") or f.get("name") or "").strip(),
        "forwarder_address": f.get("address") or "",
        "forwarder_email":   f.get("email") or "",
        "forwarder_phone":   f.get("phone") or "",
    }


def _pdf_fonts() -> tuple[str, str]:
    """Font z pełnym Unicode (ą/ę/ó...) — reużywa łańcucha fallbacków
    DejaVu/Liberation/FreeSans zarejestrowanego przez export_engine."""
    try:
        import export_engine as _ee
        return _ee.PDF_FONT, _ee.PDF_FONT_BOLD
    except Exception:
        return "Helvetica", "Helvetica-Bold"


def render_forwarding_order_pdf(data: dict) -> bytes:
    """Renderuje zlecenie spedycyjne do bajtów PDF (w pamięci, bez dysku)."""
    if not HAS_REPORTLAB:
        raise ImportError("reportlab nie jest zainstalowany. pip install reportlab")

    font, font_bold = _pdf_fonts()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.5 * cm, bottomMargin=1.5 * cm)
    styles = getSampleStyleSheet()
    s_title = ParagraphStyle("t", parent=styles["Title"], fontName=font_bold, fontSize=16)
    s_sec = ParagraphStyle("s", parent=styles["Normal"], fontName=font_bold,
                           fontSize=10, spaceBefore=12, spaceAfter=4)
    s_cell = ParagraphStyle("c", parent=styles["Normal"], fontName=font, fontSize=9)

    def _rows(pairs):
        return [[Paragraph(label, s_cell), Paragraph(str(val or "—"), s_cell)]
                for label, val in pairs]

    tbl_style = TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ])

    story = [
        Paragraph("ZLECENIE SPEDYCYJNE", s_title),
        Paragraph(f"Zamówienie: {data.get('nr_zamowienia', '')}", s_sec),
        Table(_rows([
            ("Nr zamówienia (PO)", data.get("nr_zamowienia")),
            ("Dostawca", data.get("supplier_name")),
            ("Rodzaj transportu", data.get("rodzaj_transportu")),
            ("Planowane ETD", data.get("planowane_etd")),
            ("Planowana data dostawy", data.get("data_dostawy")),
        ]), colWidths=[6 * cm, 11 * cm], style=tbl_style),
        Paragraph("Kontener", s_sec),
        Table(_rows([
            ("Numer kontenera", data.get("numer_kontenera")),
            ("ETA", data.get("eta")),
            ("Port załadunku", data.get("origin_port")),
            ("Port docelowy", data.get("dest_port")),
        ]), colWidths=[6 * cm, 11 * cm], style=tbl_style),
        Paragraph("Spedytor", s_sec),
        Table(_rows([
            ("Firma", data.get("forwarder_name")),
            ("Adres", data.get("forwarder_address")),
            ("E-mail", data.get("forwarder_email")),
            ("Telefon", data.get("forwarder_phone")),
        ]), colWidths=[6 * cm, 11 * cm], style=tbl_style),
        Spacer(1, 0.5 * cm),
    ]
    doc.build(story)
    return buf.getvalue()
