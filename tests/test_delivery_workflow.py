"""tests/test_delivery_workflow.py — logika workflow dostaw (czyste funkcje).

Model: płaski 20-krokowy stepper z gałęziami weryfikacji (proforma/artwork).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import delivery_workflow as wf


def test_status_order_and_prev():
    assert wf.STATUS_ORDER[0] == "utworzone"
    assert wf.STATUS_ORDER[-1] == "rozliczenie_transportu"
    assert len(wf.STATUS_ORDER) == 20
    assert wf.prev_status("wyslane_do_dostawcy") == "utworzone"
    assert wf.prev_status("utworzone") is None          # pierwszy etap


def test_steps_count_and_labels():
    assert len(wf.STEPS) == 20
    # każdy status z każdego kroku ma etykietę
    for st in wf.STEPS:
        for s in st["statuses"]:
            assert s in wf.STATUS_LABELS


def test_step_index_branches():
    assert wf.step_index("utworzone") == 0
    assert wf.step_index("sprawdzone_zgodne") == 4      # krok „Proforma sprawdzona"
    assert wf.step_index("sprawdzone_niezgodne") == 4
    assert wf.step_index("artwork_bledne") == 6         # krok „Artwork sprawdzony"
    assert wf.step_index("etd_sap") == 7
    assert wf.step_index("rozliczenie_transportu") == 19
    assert wf.step_index("nieznany") == -1


def test_phase_index():
    assert wf.phase_index("utworzone") == 0
    assert wf.phase_index("sprawdzone_niezgodne") == 1  # coarse faza Proforma
    assert wf.phase_index("artwork_bledne") == 2        # faza Artwork
    assert wf.phase_index("etd_sap") == 3               # faza Spedycja
    assert wf.phase_index("pzc_agencja") == 5           # faza Odprawa
    assert wf.phase_index("rozliczenie_transportu") == 7
    assert wf.phase_index("cos_dziwnego") == 0


def test_autoadvance_pl():
    assert wf.auto_advance_target("PL", "agent_przypisany") == "packing_list_otrzymany"
    assert wf.auto_advance_target("PI", "proforma_oczekiwana") is None   # PI nie auto-advance
    assert wf.auto_advance_target("PL", "rozliczenie_fiori") is None     # już za daleko


def test_missing_required_docs():
    assert wf.missing_required_docs("proforma_otrzymana", []) == ["PI"]
    assert wf.missing_required_docs("proforma_otrzymana", ["pi"]) == []      # case-insensitive
    assert wf.missing_required_docs("sad_draft", []) == ["SAD"]
    assert wf.missing_required_docs("etd_sap", []) == []                      # brak wymagań


def test_missing_slots():
    assert set(wf.missing_slots(["PO", "PI"])) == {"CI", "PL", "BL", "ARTWORK", "ARTWORK_B", "SAD"}
    assert wf.missing_slots(wf.REQUIRED_SLOT_TYPES) == []


def test_connector_pairs():
    assert ("CI", "PL") in wf.PAIRS
    assert wf.NONADJACENT_COMPARE.get("SAD") == "CI"


class TestVerifyStatuses:
    def test_status_from_comparison(self):
        assert wf.status_from_comparison("ok") == "sprawdzone_zgodne"
        assert wf.status_from_comparison("warning") == "sprawdzone_do_weryfikacji"
        assert wf.status_from_comparison("error") == "sprawdzone_niezgodne"
        assert wf.status_from_comparison("critical") == "sprawdzone_niezgodne"
        assert wf.status_from_comparison("") == "sprawdzone_zgodne"

    def test_proforma_loaded_target(self):
        assert wf.proforma_loaded_target("utworzone") == "proforma_zaladowana"
        assert wf.proforma_loaded_target("proforma_otrzymana") == "proforma_zaladowana"
        assert wf.proforma_loaded_target("sprawdzone_zgodne") is None
        assert wf.proforma_loaded_target("etd_sap") is None

    def test_new_statuses_have_labels(self):
        for s in ["proforma_zaladowana", "sprawdzone_zgodne",
                  "sprawdzone_do_weryfikacji", "sprawdzone_niezgodne",
                  "etd_sap", "zlecenie_spedycja", "agent_przypisany",
                  "packing_list_oczekiwanie", "packing_list_otrzymany",
                  "dokumenty_odprawa", "dostawa_przychodzaca",
                  "dokumenty_wyslane_odprawa", "sad_draft", "pzc_agencja",
                  "dostawa_magazyn", "rozliczenie_fiori", "rozliczenie_transportu"]:
            assert s in wf.STATUS_LABELS

    def test_branch_prev_and_next(self):
        # revert z gałęzi weryfikacji → przed sprawdzeniem (#22)
        assert wf.prev_status("sprawdzone_zgodne") == "proforma_otrzymana"
        assert wf.prev_status("sprawdzone_niezgodne") == "proforma_otrzymana"
        # wynik weryfikacji prowadzi dalej do artworku
        assert wf.next_status("sprawdzone_zgodne") == "artwork_oczekiwanie"

    def test_status_order_unbroken(self):
        assert wf.STATUS_ORDER[0] == "utworzone"
        assert wf.STATUS_ORDER[-1] == "rozliczenie_transportu"
        # gałęzie nie są w głównym spine
        assert "sprawdzone_zgodne" not in wf.STATUS_ORDER
        assert "artwork_zgodne" not in wf.STATUS_ORDER


class TestArtworkStatuses:
    def test_status_from_artwork(self):
        assert wf.status_from_artwork_comparison("ok") == "artwork_zgodne"
        assert wf.status_from_artwork_comparison("info") == "artwork_zgodne"
        assert wf.status_from_artwork_comparison("warning") == "artwork_niezgodne"
        assert wf.status_from_artwork_comparison("error") == "artwork_bledne"
        assert wf.status_from_artwork_comparison("critical") == "artwork_bledne"

    def test_artwork_awaiting_target(self):
        assert wf.artwork_awaiting_target("artwork_oczekiwanie") == "artwork_oczekiwanie"
        assert wf.artwork_awaiting_target("sprawdzone_zgodne") == "artwork_oczekiwanie"
        # poza fazą artworku (spedycja+) → nie cofamy
        assert wf.artwork_awaiting_target("etd_sap") is None
        # już jest wynik artworku → None
        assert wf.artwork_awaiting_target("artwork_zgodne") is None

    def test_artwork_chain(self):
        for s in ["artwork_oczekiwanie", "artwork_zgodne", "artwork_niezgodne", "artwork_bledne"]:
            assert s in wf.STATUS_LABELS
            assert wf.phase_index(s) == 2                       # faza Artwork
        assert wf.next_status("artwork_zgodne") == "etd_sap"
        assert wf.prev_status("artwork_bledne") == "artwork_oczekiwanie"


def test_po_vs_pi_maps_to_slots():
    assert wf.COMP_TYPE_TO_SLOTS.get("PO_VS_PI") == ("PO", "PI")
