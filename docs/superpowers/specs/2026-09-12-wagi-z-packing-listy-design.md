# Wagi netto/brutto z packing listy — projekt

*Data: 2026-09-12 · Repo: repo źródłowe (DocCompare v6) · rozszerza moduł Faktury → Excel*

## Cel

Wypełniać kolumny **Waga netto** i **Waga brutto** w Excelu danymi z **packing listy (PL)**,
a nie z faktury (faktury handlowe zwykle nie mają wag per pozycja). PL może być osobnym
plikiem PDF albo dalszą sekcją tego samego PDF co faktura.

## Decyzje (z brainstormingu)

- **Źródło wagi:** packing lista (osobny plik LUB sekcja tego samego PDF — oba przypadki).
- **Dopasowanie:** po REF (ten sam mechanizm aliasów X/X1 co przy master daty); rozjazd
  rozstrzyga operator w split-view.
- **Granularność:** waga **per pozycja (per REF)** w PL.
- **Rozpoznanie PL:** **automatyczne z treści** (kolumny wagi / nagłówek „Packing List"),
  bez tagowania przez operatora.
- **Batch = jedna przesyłka** — z packing list(y) budujemy **mapę batcha
  `REF → {weight_net, weight_gross}`** i wzbogacamy nią pozycje faktury po REF. Dzięki temu
  nie trzeba sztywno „parować dokumentów" — mapa per-REF obsługuje osobny-plik i sekcję jednym
  mechanizmem.

## Architektura

**Nowy klocek:** `packing_list_extractor.py`
- `is_packing_list(headers, raw_text) -> bool` — klasyfikacja: wykryto kolumny wagi lub
  nagłówek „Packing List"/„Lista pakowania".
- `extract_weight_map(pdf_path) -> dict[str, dict]` — z PL (plik lub sekcje) zwraca
  `{ref_norm: {"weight_net": str, "weight_gross": str}}`; sumuje wagi przy powtórzonym REF.

**Zmiana współdzielona (dodatkowa, istniejące typy nietknięte):** `table_extractor`
- `identify_columns` rozpoznaje nowe typy `weight_net`/`weight_gross`. Synonimy:
  net → „net weight", „nett weight", „n.w.", „n/w", „waga netto", „waga netto (kg)";
  gross → „gross weight", „g.w.", „g/w", „waga brutto", „waga brutto (kg)".
- `_rows_to_items` emituje `weight_net`/`weight_gross` w słowniku pozycji (dodatkowe klucze;
  porównywarka ignoruje nieznane klucze → bez regresji).

**Wzbogacenie:** `invoice_mapper`
- `map_items(db, raw_items, weight_map=None)` — po dopasowaniu REF dokłada `weight_net`/
  `weight_gross` z `weight_map` (lookup po `ref_norm`, alias sufiks-cyfrowy jak
  `find_candidates`). Brak REF w mapie → pola puste + `weight_source="brak"`.

**Orkiestracja:** `invoice_pipeline.process_job`
- Przed mapowaniem: dla batcha zbierz `weight_map` ze wszystkich plików/sekcji
  sklasyfikowanych jako PL (`packing_list_extractor.extract_weight_map`), scal.
- Przekaż `weight_map` do `map_items`.

**Upload:** `invoice_routes.upload` — auto-klasyfikuje każdy wgrany plik (invoice-table vs
packing-list) po treści; PL nie tworzy własnego joba faktury, tylko zasila mapę wag batcha.

## Przepływ

```
Upload batcha (1 faktura + 0..n PL: plik(i) i/lub sekcje)
  → klasyfikacja plik/sekcja: invoice | packing_list
  → z PL: weight_map batcha  REF→{weight_net, weight_gross}  (sumowanie per REF)
  → ekstrakcja faktury (jak dziś)
  → map_items: REF/alias + master(nazwa PL/CN/SENT/przelicznik) + weight_map(waga netto/brutto)
  → split-view: wagi widoczne i edytowalne; brak z PL = puste + flaga
  → Excel: Waga netto / Waga brutto wypełnione
```

## Obsługa błędów i przypadki brzegowe

| Sytuacja | Zachowanie |
|---|---|
| Brak PL w batchu | Wagi puste + flaga „brak z PL"; nie blokuje. |
| REF faktury nieobecny w PL | Puste wagi + flaga; luka widoczna w split-view. |
| REF w PL spoza faktury | Ignorowany. |
| Ten sam REF w PL wielokrotnie | Sumowanie wag netto/brutto per REF. |
| Alias X/X1 faktura↔PL | Ten sam `resolve` po `ref_norm` + sufiks cyfrowy. |
| PL nie sparsował się | Wagi puste + flaga; faktura przechodzi (waga nie jest bramą). |
| Liczby EU/US (`1.234,50`) | `normalizer.normalize_number()`, nie surowy `float()`. |
| Jednostki wagi (kg/g) | v1: liczba jak w PL (zakład: kg); konwersja jednostek wagi — poza v1. |

Świadome uproszczenie: waga to **dane pomocnicze**, nie brama — brak nigdy nie blokuje
zatwierdzenia (inaczej niż `ambiguous` przy REF).

## Testy

- `test_table_extractor_weight.py` — `identify_columns` rozpoznaje weight_net/weight_gross;
  `_rows_to_items` emituje je; istniejące typy (ref/qty/net) nietknięte.
- `test_packing_list_extractor.py` — `extract_weight_map` (mapa per REF, sumowanie przy
  powtórce), `is_packing_list` (po kolumnach/nagłówku), pusta mapa bez kolumn wagi.
- `test_invoice_mapper_weight.py` — wzbogacenie wagą z weight_map po REF (dokł. + alias);
  brak REF → puste + flaga.
- `test_invoice_excel_weight.py` — kolumny Waga netto/brutto wypełniane.
- Regresja: pełna suita + testy porównywarki/`table_extractor` zielone.

Split-view (edytowalne pola wagi) i realny PDF PL → weryfikacja ręczna/smoke.

## Poza zakresem (v1, YAGNI)

- Konwersja jednostek wagi (kg↔g↔lb).
- PL z tylko sumami zbiorczymi (rozbicie proporcjonalne) — zdecydowano „per pozycja".
- Sztywne parowanie dokument↔dokument po numerze/pokryciu REF (mapa per-REF w batchu to zastępuje).
- Waga jako fallback z master daty (levels_json) gdy brak PL — możliwe w v2.
