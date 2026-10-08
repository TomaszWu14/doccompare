# Workflow: skan CIPL → pliki dla agencji celnej (uzgodniony 2026-09-16)

Jedno źródło prawdy dla flow modułu Faktury → Excel / Draft SAD.
Uzupełnia specy: `docs/superpowers/specs/2026-09-12-faktury-excel-design.md`
i `2026-09-14-cipl-split-draft-sad-design.md`.

## Decyzje (2026-09-16)

| Temat | Decyzja |
|---|---|
| Dostarczenie do agencji | **Ręczny mail** — operator pobiera Excel i wysyła. Portal /agencja NIE dostaje Draft SAD (na razie). |
| Format | **Excel wystarczy** (2 arkusze wg pól WinSAD). XML/Huzar — dopiero gdy agencja poprosi. |
| Źródło PDF-ów | **Ręczny upload** w /invoices. Bez integracji z kolejką i mailem. |
| Strony-skany bez tekstu | **Zostają IGNORED** z ostrzeżeniem (1/27 stron w próbce). |
| Master data | **DO ZROBIENIA: edycja wiersza w /invoices/master + import z Excela** (dziś tylko podgląd). |
| Profile dostawców | **Pareto top-20 teraz** w /suppliers/wizard; ogon on-demand przy błędzie bramki. |
| Powiązanie z kolejką transportową | **Osobny moduł** — kontener czytany z nagłówka CI, zero sprzężenia z transport_queue. |

## Flow krok po kroku (stan kodu + oznaczone poprawki)

### Krok 0 — Master data (warunek wstępny)
- Profil dostawcy (`supplier_profiles`, `/suppliers/wizard`) — mapowanie kolumn faktury.
  **Twarda bramka: brak profilu → `status=error`, nic się nie generuje.** Naprawa: utwórz
  profil → przycisk „Ponów" (`POST /invoices/reprocess/<id>`).
- `material_master` per REF: `opis_pl`, `tariff_cn` (CN), `customs_code` (V020/V999),
  `sent`, przeliczniki UOM. Braki widać dopiero w Draft SAD jako „BRAK CN".
- Eksporter = z profilu dostawcy (bez adresu — agencja ma kontrahentów w WinSAD).
- `TODO(master-edit)`: edycja + import Excel w /invoices/master.

### Krok 1 — Upload (`/invoices/` → `POST /invoices/upload`)
- Rola `user`+. 1..N PDF → `uploads/`, batch = uuid.
- `doc_splitter.py` tnie strony: CI / PL / Proforma / B-L / other (markery tolerują
  literówki PROFOMA/PORFORMA). Zestaw kontenerowy → wiele wirtualnych dokumentów.
- PL → job `packing_list`; other/B-L → `IGNORED`; CI+proforma → przetwarzanie w tle
  (wątki-daemony). Forced supplier tylko gdy plik ma dokładnie 1 fakturę.

### Krok 2 — Ekstrakcja + mapowanie (tło)
- Kaskada `table_extractor` (pdfplumber → Camelot → Claude Vision → Mistral → Tesseract).
- Profil → role kolumn; `invoice_mapper` → REF↔master (aliasy X/X1 semantic matcherem);
  `match_status`: matched / ambiguous / unmatched.
- Z PL: wagi netto/brutto + kartony, łączone po numerze faktury z nagłówka PL
  (fallback: cały batch). `container_no` + Incoterms z nagłówka CI.

### Krok 3 — Przegląd (`/invoices/coverage` → `/invoices/review/<id>`)
- Coverage: statusy batcha, dostawcy bez profilu, licznik „zatwierdzono X/Y" do SAD.
- Split-view PDF ⟷ tabela; kolory: zielony=faktura, żółty=master, czerwony=unmatched,
  pomarańcz=ambiguous.
- **Bramka: `ambiguous` blokuje zatwierdzenie** (zły wybór = zły CN). `unmatched` nie
  blokuje — pozycja zostaje z pustymi polami master (polityka A: nic nie ginie).
- `TODO(review-ux)`, `TODO(coverage-ux)`: szlif obu ekranów.

### Krok 4 — Eksporty (per batch, tylko `confirmed`)
- Excel zbiorczy `GET /invoices/export/<batch>`: Nr faktury · Ilość · REF · Nazwa PL ·
  Wagi · Przelicznik · Kwota · CN · SENT · Status dopasowania.
- **Draft SAD** `GET /invoices/sad/<batch>` — **bramka: wszystkie CI/proformy batcha
  zatwierdzone**, inaczej 409. Arkusz Nagłówek (kontener, Incoterms, waluta, sumy,
  lista dokumentów) + arkusz Pozycje (wiersz = eksporter × CN; proforma `-S` →
  „WRAZ Z PRÓBKAMI"; brak CN → wiersz z uwagą „BRAK CN — uzupełnij master data").
- `TODO(sad-columns)`: przejść kolumny z realnym SAD-em od agencji (kraj pochodzenia,
  kody dodatkowe, opisy).

### Krok 5 — Dostarczenie
- Operator pobiera oba Excele i **wysyła agencji mailem**. Koniec flow.

## Kolejność prac (uzgodniona)

1. **QA na realnych PDF-ach** — `tools/qa_invoice_pdfs.py` na próbce z 2026-09-14
   (pliki poza repo); naprawić co się wysypie. Dowód end-to-end przed resztą.
2. **Split-view UX** — ekran review (edycja, wybór X/X1, kolory).
3. **Coverage/dashboard** — czytelność batcha, Ponów, licznik SAD.
4. **Draft SAD szlif** — kolumny vs realny SAD od agencji.
5. **Master data: edycja + import Excel** (odblokowuje samodzielne łatanie „BRAK CN").
6. Równolegle praca operatorska: **profile top-20 dostawców** w wizardzie.

## Poza zakresem (potwierdzone 2026-09-16)
XML/Huzar do WinSAD · Draft SAD w portalu /agencja · auto-mail · import z kolejki
transportowej / skrzynki · OCR do klasyfikacji stron-skanów.
