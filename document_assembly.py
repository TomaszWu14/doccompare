"""
document_assembly.py — auto-assemble the customs document set (KOLEJKA-05).

Server-side file-I/O utility only: merges a list of already-resolved PDF paths
into ONE combined PDF (pypdf PdfWriter.append per readable file, in-memory).
Selection (latest shipment_documents row per customs_checklist.CUSTOMS_DOC_TYPES)
and path resolution (app._ensure_shipment_folder realpath guard) stay in the
caller — this module never touches the DB and never mutates the input files.
Empty / all-unreadable input raises ValueError so the route can 4xx without
leaving a partial artifact.
"""
from __future__ import annotations

import io
import logging

from pypdf import PdfWriter

logger = logging.getLogger("doccompare")


def assemble_pdf(file_paths: list[str]) -> bytes:
    """Merge the given PDF files into one combined PDF and return its bytes.

    Unreadable / non-PDF paths are skipped (logged), not fatal, as long as at
    least one valid PDF remains. Raises ValueError when no readable PDF is
    given — never returns partial output.
    """
    writer = PdfWriter()
    appended = 0
    for path in file_paths or []:
        try:
            writer.append(path)
            appended += 1
        except Exception as e:
            logger.warning("document_assembly: pominięto nieczytelny plik %s: %s", path, e)
    if appended == 0:
        raise ValueError("Brak czytelnych dokumentów PDF do złożenia zestawu")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
