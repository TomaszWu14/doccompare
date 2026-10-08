# Zestawy dokumentów (CIPL) + eksport „Draft SAD" — projekt

*Data: 2026-09-14 · Repo: repo źródłowe (DocCompare v6) · Rozszerzenie modułu
Faktury → Excel (spec 2026-09-12)*

## Cel

1. Moduł Faktury → Excel ma przetwarzać **realny materiał**: PDF-y, w których faktura
   jest połączona z packing listą i proformą (CIPL), włącznie z **zestawami
   kontenerowymi** — jeden PDF z dokumentami wielu dostawców (B/L + CI+PL+PI per
   dostawca).
2. Nowy eksport per batch: **„Draft SAD"** — Excel z danymi zagregowanymi tak, jak
   agencja celna wpisuje pozycje do WinSAD (pozycja SAD = eksporter × kod CN).

## Ustalenia z brainstormingu (2026-09-14)

- **QA na realnych PDF-ach wykazało**: 4 z 5 dostarczonych plików to dokumenty
  połączone — obsługa zestawów jest warunkiem koniecznym, nie „v2".
- **Architektura: splitter stron** (podejście A) — klasyfikacja stron po tekście,
  cięcie na wirtualne dokumenty, istniejący pipeline **bez zmian**. Odrzucone:
  multi-table w kaskadzie (ryzyko regresji porównywarki), LLM-first (decyzja „bez LLM
  w pętli" z 2026-09-12 podtrzymana).
- **Proforma z sufiksem `-S` = próbki** — potwierdzone w danych (Kestrel: 1 szt ×
  0,01 USD; Corvan: 2 szt × 0,01 USD, „SAMPLES FOR RECORD WITH NO COMMERCIAL VALUE").
  Pozycje proform wchodzą do draftu SAD, agregowane do tych samych pozycji CN
  (agencja pisze „WRAZ Z PRÓBKAMI" i podaje oba numery dokumentów).
- **Format dla agencji: Excel wg pól SAD** (propozycja nasza — agencja nie narzuca
  pliku; wzorcem danych jest wydruk „Podgląd danych zgłoszenia" z WinSAD,
  SAD_1000001). Import do WinSAD (XML) — poza zakresem.
- Jednostka pracy dla agencji = **kontener/wysyłka** (batch), nie pojedyncza faktura;
  SAD agreguje nawet kilka faktur jednego eksportera w jedną pozycję CN.

## Wzorzec danych (z realnego SAD-a)

Pozycja SAD (WinSAD) potrzebuje: opis PL z ilością SZT · kod CN + kod dodatkowy
krajowy (V020/V999) · eksporter · liczba opakowań (kartony CT) · masa netto ·
wartość fakturowa pozycji (USD) · kraj pochodzenia · numery faktur i proform.
Nagłówek zgłoszenia: kontener · masa brutto · łączna wartość faktur · warunki
dostawy (np. FOB SHANGHAI) · kraj wysyłki · liczba opakowań razem.

Pokrycie w naszych danych: opis PL/CN/SENT/kod dod. (`customs_code` — kolumna już
istnieje) z `material_master`; ilości/wartości z faktur; wagi i kartony z packing
list; kontener/Incoterms z nagłówków CI; eksporter z profilu dostawcy (bez adresu —
agencja ma kontrahentów w WinSAD).

## Sekcja 1 — Architektura i komponenty

| Komponent | Rola | Nowy / zmiana |
|---|---|---|
| `doc_splitter.py` | Klasyfikacja **stron** PDF po tekście (CI / PL / Proforma / B-L / inne) → grupowanie stron w dokumenty → fizyczne cięcie na osobne PDF-y (PyMuPDF, lazy import) | **nowy** |
| `invoice_routes.upload` | Każdy wgrany plik najpierw przez splitter; dalej istniejąca logika per wycięty dokument | zmiana |
| `invoice_pipeline` | Proforma przechodzi jak faktura z `doc_kind=proforma` (układ tabeli identyczny z CI — potwierdzone w danych) | drobna zmiana |
| `packing_list_extractor` | `extract_weight_map` → dodatkowo liczba **kartonów** per REF; dopasowanie PL→faktura po numerze faktury z nagłówka PL (fallback: cały batch) | zmiana |
| `table_extractor` | Nowa rola kolumny `cartons` (synonimy: CTNS, Cartons, kartony…) | addytywna |
| `sad_draft_export.py` | Builder Excela „Draft SAD": agregacja zatwierdzonych pozycji batcha per eksporter × CN | **nowy** |
| trasa `GET /invoices/sad/<batch_id>` | Pobranie draftu; przycisk w coverage z gatingiem | **nowa** |

Kluczowa własność: istniejący pipeline (profile obowiązkowe, gate, master, split-view,
confirm) działa bez zmian — dostaje mniejsze, jednorodne PDF-y. Batch = zestaw
kontenerowy.

## Sekcja 2 — Przepływ danych

1. **Upload** 1..N plików → splitter: strona z nagłówkiem typu dokumentu zaczyna nowy
   dokument; strona bez nagłówka jest doklejana do poprzedniego (wielostronicowe
   faktury). Markery odporne na literówki z realnych danych („PROFOMA", „PORFORMA").
   Strona bez warstwy tekstu → `other` (pomijana z ostrzeżeniem; bez OCR do
   klasyfikacji w v1 — kaskada OCR nadal działa wewnątrz dokumentów przy parsingu).
2. Per wycięty dokument: PL → job `packing_list`; CI i Proforma → pipeline faktury
   (gate profilu bez zmian). `container_no` czytany z nagłówka („Container No.:" —
   obecny na każdym CI w próbce).
3. Wagi + kartony z PL łączone z fakturą po numerze faktury z nagłówka PL; review
   i confirm w split-view jak dotychczas (split-view pokazuje wycięty PDF dokumentu).
4. Eksporty per batch: dotychczasowy Excel **oraz** nowy Draft SAD.

## Sekcja 3 — Kontrakt „Draft SAD" (Excel, 2 arkusze)

**Arkusz „Nagłówek"**: Kontener · Warunki dostawy (Incoterms + port) · Kraj wysyłki ·
Waluta · Wartość faktur razem · Masa brutto razem · Liczba opakowań razem · Lista
dokumentów (nr faktury/proformy + dostawca + status).

**Arkusz „Pozycje"** (wiersz = eksporter × CN, jak pozycja SAD):
`Lp · Eksporter · Kod CN · Kod dod. (customs_code) · Opis PL [+ „WRAZ Z PRÓBKAMI"
gdy w agregacie jest proforma-**próbka**] · Ilość SZT (suma po przeliczniku UOM) ·
Masa netto · Kartony · Wartość [USD] · Kraj poch. (v1: kraj wysyłki CN) · Nr faktur ·
Nr proform · SENT · Uwagi`. Wszystkie proformy (próbki i nie) wchodzą do `Nr proform`
i sumują ilość/wartość — etykieta „WRAZ Z PRÓBKAMI" dotyczy tylko dokumentu-próbki
(heurystyka: numer proformy kończy się na `-S`, albo na `S` gdy jego numer bazowy
bez tego `S` istnieje w batchu jako numer faktury CI).

Pozycje bez CN (unmatched w master) → osobne wiersze z uwagą „BRAK CN — uzupełnij
master data" (polityka A: nic nie ginie). Pozycja bez przelicznika do SZT → ilość
w jednostce źródłowej + uwaga.

## Sekcja 4 — Model danych (idempotentne `add_column`)

- `invoice_jobs`: `doc_kind` TEXT (invoice|proforma|packing_list|other, default
  'invoice') · `source_file` TEXT · `page_from` INT · `page_to` INT ·
  `container_no` TEXT · `delivery_terms` TEXT.
- `invoice_items`: `cartons` TEXT.
- Status `InvoiceJobStatus.PACKING_LIST` zostaje dla wstecznej zgodności; typ
  dokumentu przejmuje `doc_kind`. Status `InvoiceJobStatus.IGNORED` — B/L, strona-skan
  itp., poza pipeline'em faktur (bez profilu/akcji operatora).

## Sekcja 5 — Obsługa błędów i przypadki brzegowe

| Sytuacja | Zachowanie |
|---|---|
| Brak profilu jednego z dostawców zestawu | Tylko ten dokument `error`; reszta batcha przetwarzana (już tak działa) |
| Strona-skan bez warstwy tekstu | `other` + ostrzeżenie w coverage |
| Literówki w nagłówkach (PROFOMA/PORFORMA) | Markery klasyfikacji tolerują znane warianty |
| PL bez numeru faktury w nagłówku | Wagi/kartony fallback na cały batch (jak dziś) |
| Różne numery kontenera w jednym batchu | Ostrzeżenie w arkuszu Nagłówek |
| Draft SAD przy niezatwierdzonych fakturach | Przycisk zablokowany + licznik „zatwierdzono X/Y" (niepełny draft = błędny SAD) |
| Kilka faktur jednego eksportera w batchu | Agregowane do wspólnych pozycji CN (jak robi agencja) |
| `customs_code` pusty w master | Kolumna Kod dod. pusta — agencja uzupełnia |
| Dokument z błędem (`status=error`) | Akcja „Ponów" (`POST /invoices/reprocess/<job_id>`) po usunięciu przyczyny (np. utworzeniu profilu dostawcy) — resetuje status na `uploaded` i wznawia przetwarzanie |

## Sekcja 6 — Testy + QA na realnych PDF-ach

**Unit (pytest, czysta logika, bez PDF/ML):**
- `test_doc_splitter.py` — klasyfikacja stron z tekstów syntetycznych (CI/PL/
  proforma + literówki, B/L, strona pusta); grupowanie stron wielostronicowej faktury;
  wyznaczanie zakresów cięcia.
- `test_packing_list_extractor.py` (rozszerzenie) — kartony w weight_map; łączenie
  po numerze faktury.
- `test_sad_draft_export.py` — agregacja eksporter × CN (w tym: dwie faktury jednego
  eksportera; proforma dokleja „WRAZ Z PRÓBKAMI" i sumuje ilość); unmatched jako
  osobny wiersz z uwagą; kontrakt kolumn obu arkuszy; sumy nagłówka.
- `test_invoice_pipeline.py` (rozszerzenie) — `doc_kind=proforma` przechodzi pipeline;
  gating eksportu SAD (niezatwierdzone blokują).

**QA-runner `tools/qa_invoice_pdfs.py`** (ręczny, poza CI — wymaga PyMuPDF/pdfplumber):
przepuszcza katalog PDF-ów przez splitter + pipeline i raportuje per plik: wykryte
dokumenty (typ, strony, dostawca, nr faktury), pozycje per dokument, pokrycie REF
w master, braki profili. Oczekiwania dla próbki z 2026-09-14 (6 plików, **poza
repo** — realne dane handlowe, nie commitować):
- `4500000101-0001_CIPL.pdf` → 3 dokumenty (CI, PL, proforma), Kestrel
- `CI-E0001-S.pdf` → 1 dokument (proforma), Brightline Medical
- `4500000102-1_CIPL.pdf` → 2 dokumenty (CI, PL)
- `ABCU1234567_ETD.pdf` → 19 stron: B/L + 1 skan (`other`) + 17 dokumentów CI/PL/PI
  (5 dostawców; Corvan z 2 fakturami, VEXTO bez proformy)
- `clearance_docs_for_E0001.pdf` → 2 dokumenty (CI, PL)
- `SAD_1000001.pdf` → wzorzec danych (nie wejście pipeline'u)

**DoD repo**: `python -m compileall` czysty + `python -m pytest tests/ -q` zielony;
wyniki QA-runnera pokazane przed ogłoszeniem „gotowe".

**Założenie operacyjne**: profile dostawców z próbki (Kestrel, VEXTO, Corvan,
Larkspur, Tidewell Trading, Brightline Medical, Fernhill Trading) prawdopodobnie nie
istnieją — QA to wykaże; tworzenie profili to praca operatorska w wizardzie
(`/suppliers/wizard`), nie kod.

## Poza zakresem (YAGNI)

- Import bezpośrednio do WinSAD (XML/Huzar) — dopiero gdy agencja poprosi.
- OCR do klasyfikacji stron-skanów (klasyfikacja `other` wystarcza; w próbce 1/27 stron).
- Adres eksportera w drafcie (agencja ma bazę kontrahentów w WinSAD).
- Kursy walut / przeliczenia PLN (agencja liczy w WinSAD).
- Automatyczne wykrywanie kraju pochodzenia z treści („COUNTRY OF ORIGIN") — v1
  wpisuje kraj wysyłki CN; do rewizji gdy pojawi się dostawca spoza Chin.
