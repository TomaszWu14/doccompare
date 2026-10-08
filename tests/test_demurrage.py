"""Testy demurrage.py — czysta logika ryzyka postojowego.

Zegar demurrage: rozładunek→odbiór; detention: odbiór→zwrot pustego.
Poziomy: 🟢 >3 dni do końca free time, 🟡 ≤3 dni, 🔴 po terminie.
"""
from datetime import date
import demurrage as dm


# ── parse_date ────────────────────────────────────────────────────────────────

class TestParseDate:
    def test_iso_plain(self):
        assert dm.parse_date("2026-06-10") == date(2026, 6, 10)

    def test_iso_with_time_zone(self):
        assert dm.parse_date("2026-06-10T14:30:00Z") == date(2026, 6, 10)

    def test_eu_slash(self):
        assert dm.parse_date("10/06/2026") == date(2026, 6, 10)

    def test_eu_dot(self):
        assert dm.parse_date("10.06.2026") == date(2026, 6, 10)

    def test_empty_and_garbage(self):
        assert dm.parse_date("") is None
        assert dm.parse_date("brak") is None

    def test_passthrough_date(self):
        d = date(2026, 1, 1)
        assert dm.parse_date(d) == d


# ── wyłuskiwanie kamieni milowych ze zdarzeń (oba formaty API) ────────────────

class TestExtractMilestones:
    def test_safecube_format(self):
        events = [
            {"date": "2026-06-01", "desc": "Vessel departure — Shanghai"},
            {"date": "2026-06-20", "desc": "Discharged from vessel — Gdansk"},
            {"date": "2026-06-22", "desc": "Gate out full — Gdansk"},
        ]
        m = dm.extract_milestone_dates(events)
        assert m["discharge"] == date(2026, 6, 20)
        assert m["gate_out"] == date(2026, 6, 22)
        assert m["empty_return"] is None

    def test_17track_format(self):
        events = [
            {"a": "2026-06-21", "z": "Empty returned to depot"},
            {"a": "2026-06-20", "z": "Container discharged"},
        ]
        m = dm.extract_milestone_dates(events)
        assert m["discharge"] == date(2026, 6, 20)
        assert m["empty_return"] == date(2026, 6, 21)

    def test_earliest_discharge_wins(self):
        # Dwa zdarzenia 'discharge' — start zegara to najwcześniejsze
        events = [
            {"date": "2026-06-25", "desc": "Discharged at transshipment"},
            {"date": "2026-06-20", "desc": "Discharged from vessel"},
        ]
        assert dm.extract_milestone_dates(events)["discharge"] == date(2026, 6, 20)

    def test_no_matching_events(self):
        events = [{"date": "2026-06-01", "desc": "Booking confirmed"}]
        assert dm.extract_milestone_dates(events) == {
            "discharge": None, "gate_out": None, "empty_return": None}


# ── compute_phase: poziomy i koszt ───────────────────────────────────────────

class TestComputePhase:
    TODAY = date(2026, 6, 20)

    def test_inactive_when_no_start(self):
        p = dm.compute_phase(None, None, 14, 3, 50, self.TODAY)
        assert p["active"] is False and p["level"] == "none"

    def test_green_plenty_of_time(self):
        # rozładunek 5 dni temu, free 14 → zostało 9 dni → zielony
        p = dm.compute_phase(date(2026, 6, 15), None, 14, 3, 50, self.TODAY)
        assert p["level"] == "green"
        assert p["days_used"] == 5 and p["days_remaining"] == 9

    def test_amber_within_warn_window(self):
        # 12 dni temu, free 14 → zostały 2 dni (≤3) → żółty
        p = dm.compute_phase(date(2026, 6, 8), None, 14, 3, 50, self.TODAY)
        assert p["level"] == "amber" and p["days_remaining"] == 2

    def test_red_over_deadline_with_cost(self):
        # 20 dni temu, free 14 → 6 dni po terminie → czerwony, koszt 6×50
        p = dm.compute_phase(date(2026, 5, 31), None, 14, 3, 50, self.TODAY)
        assert p["level"] == "red"
        assert p["over_days"] == 6
        assert p["cost"] == 300.0

    def test_closed_within_free_time_is_green(self):
        # odebrany w 10 dni przy free 14 → zamknięte, zielone, brak kosztu
        p = dm.compute_phase(date(2026, 6, 1), date(2026, 6, 11), 14, 3, 50, self.TODAY)
        assert p["closed"] is True and p["level"] == "green" and p["cost"] == 0.0

    def test_closed_over_free_time_is_red_with_cost(self):
        # trzymany 20 dni przy free 14 → zamknięte, czerwone, koszt 6×50
        p = dm.compute_phase(date(2026, 6, 1), date(2026, 6, 21), 14, 3, 50, self.TODAY)
        assert p["closed"] is True and p["level"] == "red" and p["cost"] == 300.0


# ── assess_container: złożenie demurrage + detention ─────────────────────────

class TestAssessContainer:
    TODAY = date(2026, 6, 20)

    def test_in_transit_not_yet_discharged(self):
        out = dm.assess_container({"discharge": None}, today=self.TODAY)
        assert out["overall_level"] == "none" and out["at_risk"] is False

    def test_demurrage_running_detention_not_started(self):
        dates = {"discharge": date(2026, 6, 8), "gate_out": None, "empty_return": None}
        out = dm.assess_container(dates, today=self.TODAY)  # 12 dni, free 14 → żółty
        assert out["demurrage"]["level"] == "amber"
        assert out["detention"]["active"] is False
        assert out["overall_level"] == "amber" and out["at_risk"] is True

    def test_overall_is_worst_of_phases(self):
        # demurrage zamknięte w terminie (zielone), detention po terminie (czerwone)
        dates = {
            "discharge": date(2026, 5, 20), "gate_out": date(2026, 5, 25),
            "empty_return": None,
        }
        out = dm.assess_container(
            dates,
            settings={"demurrage_free_days": 14, "detention_free_days": 7,
                      "detention_rate_per_day": 40},
            today=self.TODAY,
        )
        assert out["demurrage"]["level"] == "green"   # 5 dni < 14
        assert out["detention"]["level"] == "red"     # 26 dni > 7
        assert out["overall_level"] == "red"
        assert out["total_cost"] == out["detention"]["cost"] > 0

    def test_settings_override_defaults(self):
        dates = {"discharge": date(2026, 6, 14), "gate_out": None}
        # free 5 → 6 dni użyte → po terminie (gdyby default 14 byłby zielony)
        out = dm.assess_container(dates, settings={"demurrage_free_days": 5},
                                  today=self.TODAY)
        assert out["demurrage"]["level"] == "red"
