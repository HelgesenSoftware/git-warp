import argparse
import logging
import os
import secrets
import socket
import subprocess
import sys
import webbrowser

import flask.cli

import git_history.backend
from git_history.rest_api import create_app
from git_history.backend import GitHistory, ErrorResponse


def _get_history_index(gh):
    """Get branch history and current HEAD index. Exits on error."""
    if gh._in_rebase():
        print("error: rebase in progress", file=sys.stderr)
        sys.exit(1)
    history = gh._list_branch_history()
    if not history:
        print("error: no history available", file=sys.stderr)
        sys.exit(1)
    head = gh._resolve_commit("HEAD")
    hashes = [e.commit_hash for e in history]
    try:
        idx = hashes.index(head)
    except ValueError:
        print("error: HEAD not in branch history", file=sys.stderr)
        sys.exit(1)
    return history, idx


def _set_history_index(gh, history, target_idx):
    """Move HEAD to a specific history index. Exits on error."""
    if target_idx < 0 or target_idx >= len(history):
        print(f"error: target index {target_idx} out of range (0–{len(history)-1})", file=sys.stderr)
        sys.exit(1)
    result = gh.reset(history[target_idx].commit_hash)
    if isinstance(result, ErrorResponse):
        print(f"error: {result.error}: {result.message}", file=sys.stderr)
        sys.exit(1)
    print(f"At history index {target_idx}.")


def main():
    parser = argparse.ArgumentParser(
        description="Rewrite branch history with unlimited undo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=0,
                        help="TCP port for the server (default: auto-assigned)")
    parser.add_argument("--clear-log", action="store_true",
                        help="delete the log file and exit")
    parser.add_argument("--dark", action="store_true",
                        help="enable dark mode")
    parser.add_argument("--undo", nargs="?", const=1, type=int, metavar="N",
                        help="undo the last N operations (default: 1)")
    parser.add_argument("--redo", nargs="?", const=1, type=int, metavar="N",
                        help="redo the next N operations (default: 1)")
    args = parser.parse_args()

    if args.clear_log:
        if git_history.backend._LOG_PATH.exists():
            git_history.backend._LOG_PATH.unlink()
            print(f"Deleted {git_history.backend._LOG_PATH}")
        else:
            print(f"No log file at {git_history.backend._LOG_PATH}")
        sys.exit(0)

    # Require git >= 2.26.
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
        parts = r.stdout.strip().split()  # "git version X.Y.Z"
        version_parts = parts[2].split(".")
        major, minor = int(version_parts[0]), int(version_parts[1])
        if (major, minor) < (2, 26):
            raise ValueError("version too old")
    except (FileNotFoundError, IndexError, ValueError):
        # Git not found, unknown version, or version too old.
        print("fatal: git >= 2.26 required", file=sys.stderr)
        sys.exit(1)

    # Must be inside a git repo.
    r = subprocess.run(["git", "rev-parse", "--git-dir"],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        print("fatal: not a git repository", file=sys.stderr)
        sys.exit(1)

    # Reject detached HEAD.
    r = subprocess.run(["git", "symbolic-ref", "--quiet", "HEAD"],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        print("fatal: HEAD is detached; checkout a branch first",
              file=sys.stderr)
        sys.exit(1)

    # Run as command line tool for undo/redo
    if args.undo:
        gh = GitHistory(os.getcwd())
        history, idx = _get_history_index(gh)
        _set_history_index(gh, history, idx + args.undo)
        sys.exit(0)
    if args.redo:
        gh = GitHistory(os.getcwd())
        history, idx = _get_history_index(gh)
        _set_history_index(gh, history, idx - args.redo)
        sys.exit(0)
    # Run web server UI
    port = args.port
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

    token = secrets.token_urlsafe(24)
    repo_path = os.getcwd()

    app = create_app(repo_path, token)

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    flask.cli.show_server_banner = lambda *_: None

    url = f"http://127.0.0.1:{port}/?t={token}" + ("&dark=1" if args.dark else "")
    print(f"git-history running at {url}  —  Ctrl+C to quit")
    webbrowser.open(url)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=False)


if __name__ == "__main__":
    main()
