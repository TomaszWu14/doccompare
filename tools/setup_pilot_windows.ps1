# Pilot DocCompare na firmowym komputerze (Windows, BEZ uprawnien admina).
# Uruchom raz w katalogu repo:  powershell -ExecutionPolicy Bypass -File tools\setup_pilot_windows.ps1
# Potem app startuje sama przy kazdym logowaniu; recznie: tools\run_pilot.cmd

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $repo

# 1. venv + zaleznosci (prod subset — bez ciezkiego ML)
if (-not (Test-Path "$repo\.venv")) { python -m venv "$repo\.venv" }
# trusted-host: firmowe proxy podmienia certyfikat TLS i pip nie ma jego CA w store.
# ponytail: obejscie na czas pilota; docelowo IT podaje firmowy CA (pip config set global.cert <plik.pem>)
& "$repo\.venv\Scripts\pip.exe" install `
    --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org `
    -r requirements-prod.txt waitress

# 2. .env minimalny (SQLite lokalnie), nie nadpisuje istniejacego
if (-not (Test-Path "$repo\.env")) {
    $secret = -join ((1..48) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
    "SECRET_KEY=$secret`nSQLITE_PATH=instance/doccompare.db`nDISABLE_QWEN=1`nLOG_DIR=logs" |
        Out-File "$repo\.env" -Encoding utf8
}
New-Item -ItemType Directory -Force "$repo\instance", "$repo\logs" | Out-Null

# 3. migracje
& "$repo\.venv\Scripts\python.exe" migrate_db.py

# 4. skrypt startowy (waitress, port 8080, dostepny w sieci jesli firewall pusci)
@"
@echo off
cd /d $repo
.venv\Scripts\waitress-serve.exe --host=0.0.0.0 --port=8080 --threads=8 app:app >> logs\pilot.log 2>&1
"@ | Out-File "$repo\tools\run_pilot.cmd" -Encoding ascii

# 5. autostart przy logowaniu — skrot w folderze Autostart uzytkownika.
# (Register-ScheduledTask odpada: na firmowych politykach zwykly user dostaje "Odmowa dostepu")
$startup = [Environment]::GetFolderPath("Startup")
$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut("$startup\DocComparePilot.lnk")
$lnk.TargetPath = "$repo\tools\run_pilot.cmd"
$lnk.WorkingDirectory = $repo
$lnk.WindowStyle = 7          # zminimalizowane
$lnk.Save()

Write-Host ""
Write-Host "OK. Start reczny: tools\run_pilot.cmd  |  potem: http://localhost:8080/login"
Write-Host "Z innych komputerow: http://$(hostname):8080 (jesli zapora przepusci port 8080)."
