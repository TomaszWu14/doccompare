"""tests/test_backup_scripts.py — CI guardrail for the backup/rotation scripts.

Pure file-inspection tests: no `import app`, no DB driver, no ML deps — keeps
this green under CI's minimal dependency set (see .github/workflows/ci.yml).
Verifies structural properties of scripts/backup_db.sh and
scripts/rotate_backups.sh, not runtime behavior (that needs a real Postgres
and is proven at end-of-phase UAT, see 01-01-PLAN.md flagged_assumptions).
"""
import os
import shutil
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_SCRIPT = os.path.join(REPO_ROOT, "scripts", "backup_db.sh")
ROTATE_SCRIPT = os.path.join(REPO_ROOT, "scripts", "rotate_backups.sh")

# On Windows dev machines, `shutil.which("bash")` can resolve to the
# System32 WSL-relay shim instead of a real bash (Git Bash) — that shim
# fails with a WSL launcher error, not a script syntax error. Probe a
# short candidate list and use the first one that actually executes.
_BASH_CANDIDATES = [
    shutil.which("bash"),
    r"C:\Program Files\Git\usr\bin\bash.exe",
    "/usr/bin/bash",
    "/bin/bash",
]


def _find_working_bash():
    for candidate in _BASH_CANDIDATES:
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            r = subprocess.run([candidate, "-c", "exit 0"], capture_output=True, text=True)
        except OSError:
            continue
        if r.returncode == 0:
            return candidate
    return None


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_backup_script_exists_and_nonempty():
    assert os.path.isfile(BACKUP_SCRIPT)
    assert os.path.getsize(BACKUP_SCRIPT) > 0


def test_rotate_script_exists_and_nonempty():
    assert os.path.isfile(ROTATE_SCRIPT)
    assert os.path.getsize(ROTATE_SCRIPT) > 0


def test_backup_script_syntax_valid():
    bash = _find_working_bash()
    if not bash:
        return  # no usable bash in this environment — skip subcheck
    result = subprocess.run([bash, "-n", BACKUP_SCRIPT], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_rotate_script_syntax_valid():
    bash = _find_working_bash()
    if not bash:
        return
    result = subprocess.run([bash, "-n", ROTATE_SCRIPT], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_backup_script_uses_pg_dump_custom_format():
    content = _read(BACKUP_SCRIPT)
    assert "pg_dump" in content
    assert "-Fc" in content
    assert "DATA_DIR" in content


def test_backup_script_writes_atomically():
    content = _read(BACKUP_SCRIPT)
    assert ".tmp" in content
    assert "mv " in content


def test_rotate_script_enforces_retention():
    content = _read(ROTATE_SCRIPT)
    assert "14" in content
    # Monthly (1st-of-month) preservation logic.
    assert "01" in content
    assert "monthly" in content.lower() or "MONTHLY" in content
