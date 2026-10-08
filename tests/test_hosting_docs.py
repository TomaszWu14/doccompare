"""tests/test_hosting_docs.py — CI guardrail for hosting-doc consistency (REL-04).

Pure file-inspection tests: no `import app`, no DB driver, no ML deps — keeps
this green under CI's minimal dependency set (see .github/workflows/ci.yml).
Ensures render.yaml stays deleted, CLAUDE.md/docs/DOKUMENTACJA_IT.md describe
Coolify/Hetzner as the sole hosting target with no stale `render.yaml`
reference, no documented env var was lost (LOG_DIR), and README.md exists.
"""
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAUDE_MD = os.path.join(REPO_ROOT, "CLAUDE.md")
DOKUMENTACJA_IT = os.path.join(REPO_ROOT, "docs", "DOKUMENTACJA_IT.md")
README = os.path.join(REPO_ROOT, "README.md")
RENDER_YAML = os.path.join(REPO_ROOT, "render.yaml")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_render_yaml_deleted():
    assert not os.path.exists(RENDER_YAML), "render.yaml should have been removed (D-10)"


def test_claude_md_mentions_coolify_and_no_render_yaml():
    text = _read(CLAUDE_MD)
    assert "Coolify" in text
    assert "render.yaml" not in text


def test_claude_md_preserves_log_dir_doc():
    assert "LOG_DIR" in _read(CLAUDE_MD)


def test_dokumentacja_it_mentions_coolify_and_no_render_yaml():
    text = _read(DOKUMENTACJA_IT)
    assert "Coolify" in text
    assert "render.yaml" not in text


def test_readme_exists_and_mentions_coolify():
    assert os.path.exists(README), "README.md should exist at repo root"
    assert "Coolify" in _read(README)
