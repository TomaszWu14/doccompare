# DocCompare v6 — Backup i procedura odtwarzania

> Autorytatywne źródło procedury backupu i odtwarzania bazy danych oraz plików
> `DATA_DIR`. `docs/DOKUMENTACJA_IT.md` §9 wskazuje na ten dokument zamiast
> powielać treść. Dotyczy produkcji: Coolify na Hetznerze (patrz `README.md`,
> `docs/DOKUMENTACJA_IT.md` §5).

---

## 1. Backup na VPS (nocny)

Zaplanowane zadanie Coolify (scheduled task) uruchamia co noc na VPS:

1. **`scripts/backup_db.sh`** — czyta `DATABASE_URL` i `DATA_DIR` ze środowiska
   (normalizuje `postgres://` → `postgresql://`, tak jak `check_db.py`), a
   następnie:
   - `pg_dump -Fc "$DATABASE_URL" -f db_<YYYYMMDD>.dump` — **format custom**
     (`-Fc`), skompresowany, odtwarzalny wyłącznie przez `pg_restore` (nie
     `psql`). Baza produkcyjna to Coolify‑Postgres na tym samym VPS (D‑01) —
     nie ma tu snapshotów dostawcy zarządzanego, tylko `pg_dump`.
   - `tar -czf data_<YYYYMMDD>.tar.gz` z katalogu `DATA_DIR` (masterowe PDF‑y
     artworków). **Zakres backupu to wyłącznie baza + `DATA_DIR` (D‑05)** —
     `uploads/` (pliki tymczasowe/robocze) **nigdy** nie jest backupowany, tak
     samo jak `SECRET_KEY` / `instance/.secret_key`.
   - Oba zapisy są **atomowe**: skrypt pisze do `*.tmp`, a plik docelowy
     powstaje dopiero przez `mv` po sukcesie — żaden proces czytający katalog
     backupów nigdy nie zobaczy częściowego pliku.
   - Katalog docelowy: `BACKUP_DIR` (domyślnie `${DATA_DIR}/backups`, ostatecznie
     `/data/backups`) — również na trwałym wolumenie.
2. **`scripts/rotate_backups.sh`** — retencja **14 kopii dobowych + 3
   miesięczne** (kopie z 1. dnia miesiąca, D‑04). Usuwa pary `db_<stamp>.dump`
   / `data_<stamp>.tar.gz` spoza zbioru zachowywanych znaczników czasu. Czysty
   bash (`ls`/`sed`/`grep`), bez nowej zależności.

Oba skrypty są objęte testem strukturalnym `tests/test_backup_scripts.py`
(składnia `bash -n`, obecność `pg_dump -Fc`, zapis atomowy, retencja 14+3).

## 2. Kopia offsite — pull z firmy przez SSH

Zgodnie z polityką "dane firmy zostają w firmie" (ta sama zasada co przy
danych artworków), **żadne dane produkcyjne nie trafiają do zewnętrznego
storage (Hetzner Storage Box, S3 itd.)** — offsite oznacza tu dysk/NAS
on‑premise po stronie firmy.

- **Kierunek transferu: PULL z maszyny firmowej.** Zaplanowane zadanie na
  komputerze/NAS w firmie łączy się przez SSH do VPS i pobiera pliki z
  `BACKUP_DIR` (`db_*.dump`, `data_*.tar.gz`) na dysk on‑prem (D‑02, D‑03).
- **Na VPS nie są przechowywane żadne poświadczenia firmowe** — to firma
  inicjuje połączenie kluczem, którego prywatna część nigdy nie opuszcza jej
  infrastruktury.
- **Odwracalność (koszt zmiany):** zamiana kierunku na push z VPS wymagałaby
  wystawienia publicznego endpointu/VPN po stronie firmy i przeniesienia tam
  poświadczeń — zmiana wieloelementowa i kosztowna, choć nie jednokierunkowa
  (nie jest to decyzja bez odwrotu, tylko droga do cofnięcia).

## 3. Trwałe wolumeny (D-06)

Na Coolify zamontowane są trwałe wolumeny dla:

- **`DATA_DIR`** — masterowe PDF‑y artworków; backupowane przez `backup_db.sh`
  (patrz §1).
- **`instance/`** — m.in. `instance/.secret_key` oraz `instance/queues`
  (kolejka porównania pól, `_QUEUE_PERSIST_DIR` w `artwork_comparator.py`).

Dzięki temu pliki przeżywają restart/redeploy kontenera (REL-01). `uploads/`
**pozostaje efemeryczny** celowo — to pliki robocze czyszczone po
przetworzeniu, nieobjęte backupem (patrz `docs/DOKUMENTACJA_IT.md` §6).

## 4. Próba odtworzenia (drill) — TYLKO lokalnie/dev

> **⚠️ Ten dryl NIGDY nie jest uruchamiany przeciwko bazie produkcyjnej.**
> Cel istnieje wyłącznie po to, by dowieść, że kopia jest odtwarzalna — nie po
> to, by nadpisać dane produkcyjne. Zawsze cel to lokalny/Docker Postgres.

Kroki (D-08, D-09):

1. Odpal lokalny Postgres, np. Docker: `docker run --name dc-restore-drill -e POSTGRES_PASSWORD=drill -p 5433:5432 -d postgres:16`.
2. Skopiuj najnowszy dump z `BACKUP_DIR` (`db_<YYYYMMDD>.dump`) na maszynę z
   Dockerem.
3. Odtwórz dump do świeżej bazy w kontenerze:
   `pg_restore -h localhost -p 5433 -U postgres -d postgres --clean --if-exists db_<YYYYMMDD>.dump`
   (dostosuj nazwę bazy/parametry do configu kontenera).
4. Ustaw `DATABASE_URL` w środowisku shell na ten lokalny Postgres
   (`postgresql://postgres:drill@localhost:5433/postgres`).
5. Uruchom `python migrate_db.py` — **oczekiwany wynik: no-op** (schemat już
   jest w dumpie; skrypt jest idempotentny, więc nie powinien nic zmienić ani
   nic zepsuć).
6. Uruchom `python check_db.py` — zapisz liczbę tabel oraz (opcjonalnie) liczbę
   wierszy w kilku kluczowych tabelach jako "smoke check" integralności
   (D‑09).
7. Odpal aplikację (`python app.py`) wskazując na ten sam `DATABASE_URL`,
   zaloguj się kontem testowym i sprawdź, że lista dokumentów/dostaw się
   renderuje.
8. Zapisz wynik drylu (data, liczba tabel, status logowania) — patrz checklista
   w §5. Weryfikacja jest ręczna, powtarzana przy większych zmianach schematu —
   nie ma automatycznego skryptu ani harmonogramu kalendarzowego (D‑09).

## 5. Checklista operatora

- [ ] `pg_restore` zakończony bez błędów krytycznych
- [ ] `python migrate_db.py` — brak zmian (no-op)
- [ ] `python check_db.py` — liczba tabel zgodna z oczekiwaną, zapisana
- [ ] Logowanie do aplikacji działa, lista dokumentów się renderuje
- [ ] Wynik drylu (data + status) odnotowany (np. w STATE.md / notatce operacyjnej)
- [ ] Backup nocny (`backup_db.sh`) i rotacja (`rotate_backups.sh`) zaplanowane
      jako zadanie cykliczne Coolify
- [ ] Zadanie pull po stronie firmy (SSH) skonfigurowane i uruchamiane cyklicznie
- [ ] Trwałe wolumeny `DATA_DIR` i `instance/` potwierdzone w konfiguracji Coolify
