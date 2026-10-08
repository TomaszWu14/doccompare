"""tests/test_backup_restore_runbook.py — CI guardrail for the backup/restore
runbook (REL-03).

Pure file-inspection test: no `import app`, no DB driver, no ML deps — keeps
this green under CI's minimal dependency set (see .github/workflows/ci.yml).
docs/** is path-ignored for CI *triggering*, so this .py file is what actually
carries the check into a running CI job. Guards docs/BACKUP_RESTORE.md against
silently regressing to an empty/placeholder stub.
"""
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNBOOK = os.path.join(REPO_ROOT, "docs", "BACKUP_RESTORE.md")

REQUIRED_ANCHORS = [
    "backup_db.sh",
    "rotate_backups.sh",
    "pg_restore",
    "migrate_db.py",
    "check_db.py",
    "DATA_DIR",
    "instance/",
]


def _read():
    with open(RUNBOOK, encoding="utf-8") as f:
        return f.read()


def test_runbook_exists_and_nonempty():
    assert os.path.isfile(RUNBOOK), "docs/BACKUP_RESTORE.md should exist"
    assert os.path.getsize(RUNBOOK) > 0


def test_runbook_contains_required_anchors():
    text = _read()
    missing = [anchor for anchor in REQUIRED_ANCHORS if anchor not in text]
    assert not missing, f"docs/BACKUP_RESTORE.md missing required anchors: {missing}"
