# Faktury → Excel — projekt modułu

*Data: 2026-09-12 · Repo: repo źródłowe (DocCompare v6)*

## Cel

Nowy, osobny moduł: wyciąga dane z faktur dostawców (najczęściej PDF, ~100 różnych
formatów) i generuje **jeden wspólny plik Excel** o stałym układzie. Master data
materiałowa (`material_master`) dokłada polską nazwę, kod celny (CN), flagę SENT i
przeliczniki jednostek. Zapis do Excela **wyłącznie po manualnym potwierdzeniu**
operatora względem oryginalnej faktury.

## Decyzje (z brainstormingu)

- **Osobny moduł**, nie rozszerzenie porównywarki. Reużywa istniejących klocków,
  nie jest przez porównywarkę wołany.
- **Dopasowanie po REF** z rozwiązywaniem aliasów (faktura ma REF `X`, u nas może być
  `X` lub `X1`) przez `semantic_matcher`. Niejednoznaczność → wybór operatora.
- **Ekstrakcja: Podejście 1** — kaskada generyczna + profil dostawcy. **Profil
  obowiązkowy: brak profilu = brak generowania** (twardy wymóg). Bez LLM w pętli.
- **Split-view** (PDF ⟷ edytowalna tabela) z podświetlaniem wg pewności/źródła.
- **Batch**: 1..N faktur; wyjście **zbiorczy Excel** + opcja per-faktura.
- Pozycja niedopasowana do master → **zostaje w Excelu** z pustymi polami master +
  flagą (polityka A, nic nie ginie).

## Kolumny Excela (stały kontrakt)

Nr faktury · Ilość · REF · Nazwa PL · Waga netto · Waga brutto · Przelicznik jednostki ·
Kwota · Kod celny (CN) · SENT · Status dopasowania

Źródła: Ilość/REF/wagi/kwota z **faktury**; Nazwa PL (`txt_short_pl`)/CN (`tariff_cn`)/
SENT (`sent`)/przelicznik z **master daty** po REF.

## Sekcja 1 — Architektura i komponenty

| Komponent | Rola | Nowy / istniejący |
|---|---|---|
| `invoice_extractor.py` | Surowa tabela pozycji z PDF | **nowy**, cienki — woła `table_extractor.py` |
| `supplier_profiles.py` | Kolumny surowej tabeli → role (REF/ilość/kwota/waga…) | istniejący |
| `invoice_mapper.py` | Faktura-REF → master-REF (alias X/X1), dokłada nazwę PL/CN/SENT/przelicznik | **nowy** |
| `material_master.py` | Źródło nazwy PL, `tariff_cn`, `sent`, przeliczników | istniejący |
| `invoice_excel_export.py` | Excel z zatwierdzonych wierszy (openpyxl) | **nowy**, cienki |
| widok Flask `/invoices/*` | Upload → kolejka → split-view → pobranie Excela | **nowy** |
| tabele `invoice_jobs` + `invoice_items` | Stan faktur i pozycji | **nowy** (wzorzec `job_progress`) |

Granica: moduł zależy od `table_extractor`, `semantic_matcher`, `material_master`,
`supplier_profiles`, `db`. Ekstrakcja batcha w wątku-daemonie. Split-view per faktura.

## Sekcja 1b — Widok przeglądowy („klasyczny" moduł)

- **Dashboard pokrycia** `/invoices/coverage`: lista dostawców — profil
  (sparametryzowany / brak), liczba rekordów master pod dostawcą, liczba przerobionych
  faktur, data ostatniej, liczba próbek użytych do profilu, wskaźnik „czystych
  przebiegów" (faktury zatwierdzone bez poprawek).
- **Przeglądarka master daty**: czytelna tabela `material_master` (REF, nazwa PL, CN,
  SENT, przeliczniki) z filtrem/szukajką. Tylko widok — nic nie duplikuje.

Cel: widać braki (dostawca bez profilu, REF bez CN/SENT) **zanim** wgrasz fakturę.

## Sekcja 2 — Przepływ danych

1. **Upload** — 1..N PDF → `uploads/`, wiersz w `invoice_jobs` (`status=uploaded`).
   Rozpoznanie dostawcy (z treści; jak nie — operator wybiera z listy).
   **Brak profilu → STOP**: „Utwórz profil dla `<dostawca>`", link do `/suppliers/wizard`.
2. **Ekstrakcja** (tło, per faktura): kaskada `table_extractor` → surowa tabela; profil
   dostawcy → mapowanie kolumn na role. `status=extracted`.
3. **Mapowanie** (`invoice_mapper`, per pozycja): faktura-REF → master-REF (dokładne;
   alias X/X1 przez `semantic_matcher`); z master: nazwa_pl, tariff_cn, sent, przelicznik;
   `match_status` = matched | ambiguous | unmatched; przeliczenie jednostek (`uom.py`).
4. **Potwierdzenie** (operator): split-view PDF ⟷ tabela. Kolory: zielone=z faktury,
   żółte=z master, czerwone=unmatched, pomarańcz=ambiguous. Operator poprawia komórki,
   wybiera wersję przy ambiguous, oznacza „pomiń". unmatched zostaje z pustymi polami
   master. „Zatwierdź" → `status=confirmed`.
5. **Excel** (`invoice_excel_export`): zatwierdzone wiersze → zbiorczy Excel (kolumna Nr
   faktury) + opcja per-faktura. `status=exported`.

## Sekcja 3 — Model danych

**`invoice_jobs`** (jedna faktura = wiersz):
`id` PK · `batch_id` · `filename` · `pdf_path` · `supplier_code` · `status`
(uploaded|extracted|confirmed|exported|error) · `error` · `invoice_number` ·
`created_at` · `updated_at`.

**`invoice_items`** (jedna pozycja = wiersz):
`id` PK · `job_id` FK · `line_no` · surowe z faktury: `raw_ref`, `qty`, `net_amount`,
`weight_net`, `weight_gross`, `uom_src`, `amount` · z master: `master_ref`, `name_pl`,
`tariff_cn`, `sent`, `uom_factor` · kontrola: `match_status`
(matched|ambiguous|unmatched), `skipped`.

Tabele idempotentne (`CREATE TABLE IF NOT EXISTS` w `migrate_db.py`). Zapamiętywanie
wyboru wersji X/X1 (tabela `ref_alias_map`) **świadomie odłożone do v2** (YAGNI —
najpierw zmierzyć jak często X/X1 realnie występuje; dodanie później nie zmienia tego
schematu).

## Sekcja 4 — Obsługa błędów i przypadki brzegowe

| Sytuacja | Zachowanie |
|---|---|
| Brak profilu dostawcy | STOP przed ekstrakcją; `status=error`; „Utwórz profil dla `<dostawca>`", link do wizarda. |
| Nie rozpoznano dostawcy | Operator wybiera dostawcę z listy przy uploadzie; potem gate profilu. |
| Kaskada nic nie wyciągnęła | `status=error`, „Nie udało się odczytać tabeli"; faktura do ręcznego ponowienia, nie blokuje batcha. |
| Pozycja niedopasowana | Zostaje (polityka A): puste pola master, `unmatched`, czerwony. |
| REF niejednoznaczny (X/X1) | `ambiguous`, pomarańcz, dropdown wyboru wersji. **Zatwierdzenie zablokowane póki jest jakiś `ambiguous`** (unmatched jest OK). |
| Brak przelicznika w master | Pole puste + flaga; ilość 1:1. |
| Liczby EU/US (`13.500,00`) | `normalizer.normalize_number()`, nie surowy `float()`. |
| Częściowy batch | Każda faktura niezależna; eksport tylko `confirmed`. |
| Duplikat faktury | Ostrzeżenie przy uploadzie, nie blokada. |

Świadoma różnica: **ambiguous blokuje** (zła wersja = zły CN/SENT = błąd celny),
**unmatched nie blokuje** (widoczna luka do uzupełnienia).

## Sekcja 5 — Testy

pytest w `tests/` (czysta logika; kaskadę i PDF mockujemy):

- `test_invoice_mapper.py` — dokładny REF; alias `X`≈`X1`; `unmatched`; `ambiguous` gdy
  X i X1 istnieją; poprawne name_pl/tariff_cn/sent.
- `test_invoice_profile_gate.py` — brak profilu → ekstrakcja nie rusza, `status=error`.
- `test_invoice_uom.py` — przelicznik z master; brak → 1:1 + flaga.
- `test_invoice_excel_export.py` — kontrakt kolumn, unmatched z pustymi polami, skipped
  pominięte, kolumna Nr faktury.
- `test_invoice_status_flow.py` — przejścia statusów; zatwierdzenie zablokowane przy
  `ambiguous`, dozwolone przy `unmatched`.

Ekstrakcja z realnego PDF i split-view (frontend) — testy ręczne. DoD: `compileall`
czysty + `pytest` zielony przed „gotowe".

## Przygotowanie danych (do planu, nie kod)

Profil = deterministyczne mapowanie kolumn, nie model ML → nie trzeba dużych liczb.
Potrzeba tylu próbek, by złapać zmienność formatu dostawcy:
- **3 faktury** dla prostego, stałego układu (większość dostawców).
- **5 faktur** dla nieregularnych (różne szablony, wielostronicowe).
- **Pareto**: ~20 dostawców ≈ ~80% wolumenu → profile dla górnej dwudziestki najpierw
  (5 próbek każdy ≈ 100 PDF-ów), długi ogon on-demand przy pierwszym kontakcie.
- **Sygnał „profil gotowy"**: 2–3 kolejne nowe faktury tego dostawcy zatwierdzone bez
  poprawek w split-view (mierzone wskaźnikiem „czystych przebiegów" na dashboardzie),
  nie sztywna liczba.

## Poza zakresem v1 (YAGNI)

- LLM-first ekstrakcja / auto-generowanie profilu (fallback rozważyć w v2 jeśli długi
  ogon dostawców okaże się uciążliwy).
- Tabela `ref_alias_map` z zapamiętywaniem wyboru X/X1.
- Automatyczny wybór wersji z treści faktury (data/rewizja).
