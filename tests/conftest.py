"""Shared test helpers for repo construction."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from make_test_repo import AUTHORS, BASE_DATE, COMMITS, create_lib_repo, init_repo, make_commit


def _ensure_persistent_test_repo() -> Path:
    """Create the standard 21-commit test repo in the root if it doesn't exist."""
    repo_root = REPO_ROOT
    repo = repo_root / ".test-repo"
    lib = repo_root / ".test-repo-lib"

    # Check if repo is already fully initialized
    if (repo / ".git").exists():
        return repo

    # Create the persistent test repo
    lib_hash1, lib_hash2 = create_lib_repo(lib)
    sub = {"url": str(lib), "hash1": lib_hash1, "hash2": lib_hash2}
    repo.mkdir(exist_ok=True)
    init_repo(repo)
    for i, (msg, author_key, files, tag) in enumerate(COMMITS):
        make_commit(repo, i, msg, author_key, files, tag, sub=sub)

    return repo


def _commit_raw(repo, relpath, data, message, author_key, day_offset):
    from datetime import timedelta
    (repo / relpath).write_bytes(data)
    subprocess.run(["git", "add", "--", relpath], cwd=str(repo),
                   check=True, capture_output=True)
    author_name, author_email = AUTHORS[author_key]
    when = (BASE_DATE + timedelta(days=day_offset)).isoformat()
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"]     = author_name
    env["GIT_AUTHOR_EMAIL"]    = author_email
    env["GIT_AUTHOR_DATE"]     = when
    env["GIT_COMMITTER_NAME"]  = author_name
    env["GIT_COMMITTER_EMAIL"] = author_email
    env["GIT_COMMITTER_DATE"]  = when
    subprocess.run(["git", "commit", "-m", message], cwd=str(repo),
                   env=env, check=True, capture_output=True)




