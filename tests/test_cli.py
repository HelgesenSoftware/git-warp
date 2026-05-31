"""Tests for the git_history.py CLI entry point.

Only the early-exit error paths are tested here; the happy path (server starts,
browser opens) requires interactive verification — see manual_test.md.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import _ensure_persistent_test_repo

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "git_history"] + args,
        capture_output=True, text=True, cwd=str(cwd),
        env={**__import__("os").environ, "PYTHONPATH": str(REPO_ROOT)},
    )


@pytest.mark.release
def test_not_a_git_repo():
    with tempfile.TemporaryDirectory() as d:
        r = _run_cli([], d)
        assert r.returncode != 0
        assert "not a git repository" in r.stderr


@pytest.mark.release
def test_detached_head():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        subprocess.run(["git", "init", "-b", "main"], cwd=d, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=d, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=d, capture_output=True)
        (d / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=d, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=d, capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                              capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "checkout", head], cwd=d, capture_output=True)
        r = _run_cli([], d)
        assert r.returncode != 0
        assert "detached" in r.stderr


def _setup_test_repo_with_history(tmpdir):
    """Clone a test repo and build reflog entries for undo/redo testing."""
    test_repo = _ensure_persistent_test_repo()
    repo = Path(tmpdir)
    subprocess.run(["git", "clone", str(test_repo), str(repo)], capture_output=True, check=True)
    subprocess.run(["git", "remote", "remove", "origin"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=repo, capture_output=True, check=True)
    return repo


def test_undo_one_commit():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = _setup_test_repo_with_history(tmpdir)
        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                     capture_output=True, text=True).stdout.strip()
        r = _run_cli(["--undo"], repo)
        assert r.returncode == 0
        assert "At history index" in r.stdout
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                    capture_output=True, text=True).stdout.strip()
        assert head_before != head_after


def test_undo_multiple_commits():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = _setup_test_repo_with_history(tmpdir)
        r = _run_cli(["--undo", "2"], repo)
        assert r.returncode == 0
        assert "At history index" in r.stdout


def test_redo_one_commit():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = _setup_test_repo_with_history(tmpdir)
        _run_cli(["--undo"], repo)
        head_before_redo = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                          capture_output=True, text=True).stdout.strip()
        r = _run_cli(["--redo"], repo)
        assert r.returncode == 0
        assert "At history index" in r.stdout
        head_after_redo = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                         capture_output=True, text=True).stdout.strip()
        assert head_before_redo != head_after_redo


@pytest.mark.release
def test_undo_too_many():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = _setup_test_repo_with_history(tmpdir)
        r = _run_cli(["--undo", "100"], repo)
        assert r.returncode != 0
        assert "error:" in r.stderr


def test_undo_redo_are_inverses():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = _setup_test_repo_with_history(tmpdir)
        original_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                       capture_output=True, text=True).stdout.strip()
        _run_cli(["--undo"], repo)
        _run_cli(["--redo"], repo)
        final_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                    capture_output=True, text=True).stdout.strip()
        assert final_head == original_head


@pytest.mark.release
def test_redo_at_tip():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = _setup_test_repo_with_history(tmpdir)
        r = _run_cli(["--redo"], repo)
        assert r.returncode != 0
        assert "error:" in r.stderr


@pytest.mark.release
def test_undo_and_redo_mutually_exclusive():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = _setup_test_repo_with_history(tmpdir)
        r = _run_cli(["--undo", "--redo"], repo)
        assert r.returncode != 0
        assert "error" in r.stderr.lower()
