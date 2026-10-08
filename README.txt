# DocCompare v6 — ACME

## Uruchomienie (pierwsza instalacja)

1. Utwórz środowisko wirtualne:
   python -m venv .venv
   .venv\Scripts\activate       (Windows)

2. Zainstaluj zależności:
   pip install -r requirements.txt

3. Migracja bazy danych:
   python migrate_db.py

4. Uruchom:
   python app.py

Aplikacja dostępna na: http://localhost:5000
Sieciowo (dla kolegów): http://[twoj-ip]:5000

## Domyślne konta
- admin / admin123
- superuser / super123
- jan.kowalski / haslo123
- anna.nowak / haslo123

## Firewall (żeby kolega mógł się połączyć)
W PowerShell jako Administrator:
  netsh advfirewall firewall add rule name="DocCompare" dir=in action=allow protocol=TCP localport=5000

## Ghostscript (opcjonalny — poprawia jakość parsowania PDF)
Pobierz ghostpcl-10.07.0-win64.zip z:
https://github.com/ArtifexSoftware/ghostpdl-downloads/releases
Rozpakuj do folderu: [projekt]\ghostscript\
(bez instalacji, bez uprawnień admina)

## Struktura projektu
app.py                  — główna aplikacja Flask
enhanced_comparator.py  — silnik porównania PO/PI/CI
table_extractor.py      — parser tabel PDF
supplier_profiles.py    — profile i wizard dostawców
column_parser.py        — zaawansowane reguły parsowania kolumn
export_engine.py        — eksport PDF i Excel
templates/              — szablony HTML
instance/               — baza danych SQLite (tworzona automatycznie)
uploads/                — pliki PDF (tworzone automatycznie)
