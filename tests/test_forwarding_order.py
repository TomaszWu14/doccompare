"""tests/test_forwarding_order.py — czysta logika zlecenia spedycyjnego
(KOLEJKA-02, Phase 5 Plan 02).

Covers:
  - required_fields: niepotwierdzone zlecenie (zgoda_wyplyniecie != 1) lub brak
    spedytora -> niepusta lista brakujących pól; potwierdzone + spedytor -> [].
  - build_forwarding_order_data: płaski dict ze wszystkimi polami dokumentu.
  - render_forwarding_order_pdf: niepuste bajty zaczynające się od %PDF.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

import forwarding_order as fo

_ZLECENIE = {
    "nr_zamowienia": "PO-2026-001",
    "supplier_name": "Eastport Medical",
    "rodzaj_transportu": "40HQ",
    "planowane_etd": "2026-10-01",
    "data_dostawy": "2026-11-15",
    "zgoda_wyplyniecie": 1,
    "forwarder_id": 7,
}

_KONTENER = {
    "numer_kontenera": "MSKU1234567",
    "typ_transportu": "40HQ",
    "eta": "2026-11-10",
    "origin_port": "Shanghai",
    "dest_port": "Gdansk",
}

_FORWARDER = {
    "name": "Jan Spedytor",
    "company": "Sped-Alfa Sp. z o.o.",
    "email": "jan@spedalfa.pl",
    "phone": "+48 123 456 789",
    "address": "ul. Portowa 1, 80-001 Gdańsk",
}


# ── required_fields ──────────────────────────────────────────────────────────

def test_required_fields_unconfirmed_order_flagged():
    row = dict(_ZLECENIE, zgoda_wyplyniecie=0)
    missing = fo.required_fields(row)
    assert "zgoda_wyplyniecie" in missing


def test_required_fields_no_forwarder_flagged():
    row = dict(_ZLECENIE, forwarder_id=None)
    missing = fo.required_fields(row)
    assert "forwarder_id" in missing


def test_required_fields_confirmed_with_forwarder_passes():
    assert fo.required_fields(dict(_ZLECENIE)) == []


def test_required_fields_empty_row_flags_both():
    missing = fo.required_fields({})
    assert "zgoda_wyplyniecie" in missing
    assert "forwarder_id" in missing


# ── build_forwarding_order_data ──────────────────────────────────────────────

def test_build_data_carries_all_document_fields():
    data = fo.build_forwarding_order_data(_ZLECENIE, _KONTENER, _FORWARDER)
    assert data["nr_zamowienia"] == "PO-2026-001"
    assert data["supplier_name"] == "Eastport Medical"
    assert data["rodzaj_transportu"] == "40HQ"
    assert data["planowane_etd"] == "2026-10-01"
    assert data["data_dostawy"] == "2026-11-15"
    assert data["numer_kontenera"] == "MSKU1234567"
    assert data["eta"] == "2026-11-10"
    assert data["forwarder_name"] == "Sped-Alfa Sp. z o.o."
    assert data["forwarder_address"] == "ul. Portowa 1, 80-001 Gdańsk"
    assert data["forwarder_email"] == "jan@spedalfa.pl"


def test_build_data_without_container_row_uses_empty_strings():
    data = fo.build_forwarding_order_data(_ZLECENIE, None, _FORWARDER)
    assert data["numer_kontenera"] == ""
    assert data["eta"] == ""
    assert data["nr_zamowienia"] == "PO-2026-001"


def test_build_data_forwarder_name_falls_back_to_person_name():
    fwd = dict(_FORWARDER, company="")
    data = fo.build_forwarding_order_data(_ZLECENIE, _KONTENER, fwd)
    assert data["forwarder_name"] == "Jan Spedytor"


# ── render_forwarding_order_pdf ──────────────────────────────────────────────

def test_render_returns_valid_pdf_bytes():
    pytest.importorskip("reportlab")
    data = fo.build_forwarding_order_data(_ZLECENIE, _KONTENER, _FORWARDER)
    pdf = fo.render_forwarding_order_pdf(data)
    assert isinstance(pdf, bytes)
    assert len(pdf) > 100
    assert pdf.startswith(b"%PDF")


def test_render_pdf_contains_order_number_text():
    pytest.importorskip("reportlab")
    pypdf = pytest.importorskip("pypdf")
    import io
    data = fo.build_forwarding_order_data(_ZLECENIE, _KONTENER, _FORWARDER)
    pdf = fo.render_forwarding_order_pdf(data)
    text = pypdf.PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert "PO-2026-001" in text
    assert "MSKU1234567" in text
