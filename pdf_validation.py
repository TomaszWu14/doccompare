"""
pdf_validation.py — walidacja przesyłanych plików PDF (magic bytes, MIME, hasło).

Wydzielone z app.py (Phase 3, plan 03-02): `_validate_pdf_upload`/`_ALLOWED_PDF_MIMES`
mają wywołania zarówno w app.py (kilkanaście miejsc), jak i w blueprints/suppliers.py
(`api_suppliers_preview_table`) — muszą żyć poza app.py, żeby blueprint nie importował
z powrotem do app (reguła: blueprint → core/db, nigdy → app).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("doccompare")

ALLOWED_PDF_MIMES = {"application/pdf", "application/x-pdf", "binary/octet-stream"}


def validate_pdf_upload(f) -> str | None:
    """Return error string if file is not a valid PDF upload, else None."""
    if not f or not f.filename:
        return "Brak pliku"
    if not f.filename.lower().endswith(".pdf"):
        return f"'{f.filename}' nie jest plikiem PDF"
    ct = (f.content_type or "").lower().split(";")[0].strip()
    if ct and ct not in ALLOWED_PDF_MIMES:
        return f"Nieprawidłowy typ MIME: {ct}"
    # Validate PDF magic bytes (%PDF header) and check for encryption
    try:
        f.stream.seek(0)
        header = f.stream.read(4)
        if not header or header != b"%PDF":
            f.stream.seek(0)
            return "Plik nie jest prawidłowym dokumentem PDF"
        # Scan PDF tail for /Encrypt entry (present in all password-protected PDFs)
        f.stream.seek(0, 2)
        file_size = f.stream.tell()
        tail_size = min(4096, file_size)
        f.stream.seek(max(0, file_size - tail_size))
        tail = f.stream.read(tail_size)
        f.stream.seek(0)
        if b"/Encrypt" in tail:
            # /Encrypt ≠ „hasło do otwarcia". Bardzo częsty przypadek: puste hasło
            # użytkownika + tylko ograniczenia właściciela (np. zakaz kopiowania) —
            # plik otwiera się normalnie. Odrzucamy WYŁĄCZNIE, gdy realnie wymaga
            # hasła do otwarcia (needs_pass). Inaczej przepuszczamy (odszyfrujemy
            # pustym hasłem przy zapisie — patrz _decrypt_pdf_inplace).
            try:
                import fitz
                f.stream.seek(0)
                _data = f.stream.read()
                f.stream.seek(0)
                _doc = fitz.open(stream=_data, filetype="pdf")
                _needs = bool(_doc.needs_pass)
                _doc.close()
                if _needs:
                    return "Plik PDF wymaga hasła do otwarcia — prześlij wersję bez hasła."
            except Exception as _ce:
                logger.warning("PDF encrypt check for '%s' failed: %s", f.filename, _ce)
                return "Plik PDF jest chroniony hasłem — prześlij wersję bez zabezpieczeń"
    except Exception as _e:
        logger.warning("PDF validation stream error for '%s': %s", f.filename, _e)
        return "Błąd odczytu pliku — prześlij ponownie"
    return None
