"""customs_checklist.py — czysta logika twardej bramki celnej (KOLEJKA-03).

Bramka na przejście do statusu 'dokumenty_wyslane_odprawa' (wysłanie dokumentów
do odprawy): każdy wymagany doc_type z checklisty dostawy (delivery_checklists)
musi mieć status DOKŁADNIE 'zatwierdzono'. W odróżnieniu od miękkiej bramki
REQUIRED_DOCS (confirm=true przepuszcza) ta jest bezwarunkowa — sprawdzana
server-side w app.py PRZED blokiem miękkiej bramki.

Moduł zawiera wyłącznie dane i czyste funkcje — żadnych efektów ubocznych;
wiersze checklisty pobiera i przekazuje wywołujący.
"""

# doc_type'y wymagane do wysłania dokumentów do odprawy celnej — podzbiór
# listy seedowanej przez app._ensure_delivery_checklist (PO, PI, CI, PL, SAD, BL).
CUSTOMS_DOC_TYPES = ("CI", "PL", "BL", "SAD")

APPROVED_STATUS = "zatwierdzono"


def missing_hard_required(checklist_rows) -> list:
    """Zwraca doc_type'y z CUSTOMS_DOC_TYPES, które NIE są 'zatwierdzono'
    (albo nie mają wiersza w checkliście). Pusta lista = bramka otwarta."""
    approved = {
        (r["doc_type"] or "").upper()
        for r in (checklist_rows or [])
        if r["status"] == APPROVED_STATUS
    }
    return [d for d in CUSTOMS_DOC_TYPES if d not in approved]
