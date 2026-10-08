"""Lekka, czysta logika ekstrakcji numerów SAP z nazw plików / treści PDF.

Wydzielona z app.py, aby była testowalna bez ciężkich zależności (pdfplumber,
torch itd.). Używana m.in. przy uploadzie dokumentu dostawy, gdzie z nazwy
pliku typu "4500000204 Inspection Report.pdf" trzeba wyłuskać numer dostawy.
"""

import re

# Numer dostawy SAP: zawsze prefiks "45", łącznie 10+ cyfr (np. 4500000204).
# (?<!\d) / (?!\d) izolują pełny ciąg cyfr, więc wzorzec działa zarówno dla
# "4500000204 Inspection Report.pdf" jak i "4500000204Raport.pdf", a nie złapie
# fragmentu dłuższego ciągu cyfr. Górna granica 18 chroni przed absurdami.
DELIVERY_NO_RE = re.compile(r'(?<!\d)(45\d{8,18})(?!\d)')


def extract_delivery_number(*sources: str) -> str:
    """Zwraca pierwszy napotkany numer dostawy ("45" + 8–18 cyfr) z podanych
    źródeł. Kolejność argumentów = priorytet (np. najpierw nazwa pliku, potem
    treść dokumentu). Zwraca '' gdy nic nie pasuje.
    """
    for src in sources:
        if not src:
            continue
        m = DELIVERY_NO_RE.search(str(src))
        if m:
            return m.group(1)
    return ""
