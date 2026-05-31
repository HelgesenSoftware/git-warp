# git-history: Specification

## Overview

A command-line tool for Windows, run from inside any git repository. It starts a local HTTP server bound to `127.0.0.1`, opens a browser, and presents a single-page UI for rewriting git history. All git operations run server-side via `subprocess`; the browser is pure UI.

Design goal: minimal, reliable code with no avoidable failure modes. Dependencies: Flask, stdlib. No build step or JS bundler.

## Project Structure

```
git-history/
├── git_history/
│   ├── __init__.py      # CLI entry point (main())
│   ├── __main__.py      # python -m git_history entry point
│   ├── backend.py       # GitHistory class and dataclasses
│   ├── rest_api.py      # Flask app factory (create_app)
│   ├── editor.py        # GIT_SEQUENCE_EDITOR / GIT_EDITOR shim
│   └── static/
│       ├── index.html
│       ├── app.js
│       ├── style.css
│       └── manual.html
```

## CLI


| Argument | Default   | Meaning |
|----------|-----------|---------|
| `--port` | auto | TCP port on `127.0.0.1`. If omitted, picks a free port. |
| `--clear-log` | — | Delete `~/.git-history.log` and exit. |
| `--undo [N]` | N=1 | Reset HEAD back N steps in the Undo Stack. No server started. |
| `--redo [N]` | N=1 | Reset HEAD forward N steps in the Undo Stack. No server started. |
| `--dark` | off | Enable dark mode in the browser UI. |

## Startup

1. Validate: git >= 2.26, in a repo, not detached HEAD.
2. Pick port (auto or `--port N`), generate 32-char token (`secrets.token_urlsafe(24)`).
3. Start Flask server on `127.0.0.1:<port>` with state starting at `HEAD~500` (or repo root if fewer commits).
4. Print `git-history running at http://127.0.0.1:<port>/?t=<token>  —  Ctrl+C to quit` and open browser.

## Shutdown

Two triggers: Ctrl+C in terminal or Quit button (`POST /api/quit`). On Ctrl+C, the server exits normally. On quit, `os._exit(0)` is called after the response.

The server never touches the repo on shutdown. Conflicted rebase state is preserved in `.git/rebase-merge`; restarting git-history picks it up.

## Security

- Server binds only to `127.0.0.1`.
- A 32-char random token is generated at startup and embedded in the launch URL as `?t=<token>`.
- `index.html` and `static/*` are served without token checks (harmless on localhost).
- `app.js`, on load: reads `t` from `window.location.search`, stores it in `sessionStorage` under `git_history_token`, then calls `history.replaceState(null, '', '/')` to strip the query string from history.
- Every `/api/*` request sends the token in the `X-Token` header. Mismatch/missing → HTTP 403 with empty body.

## Concurrency

Flask's development server (used here) is single-threaded. Requests are processed in arrival order; a long-running rebase blocks other requests until it finishes. No locks needed because there is no concurrency. The UI shows a spinner while any request is in flight and disables mutating controls.


## Working Tree Policy

Before every mutating operation the server runs:

```
git status --porcelain --untracked-files=no
```

If the output is non-empty, the operation is refused.

The UI shows a persistent banner when the working tree is dirty and disables all rebase, reset, reword, and delete actions until the next refresh reports it clean.

## REST API

### Endpoints

- `GET /` — serves `index.html`.
- `GET /static/<path>` — serves static files.
- `GET /api/state` — returns full state object.
- `POST /api/stash` — runs `git stash push`.
- `POST /api/stash/pop` — runs `git stash pop`.
- `POST /api/rebase/move` — reorder commits. Request body: `{ "order": ["<hash>", ...], "select_index": <int> }`
- `POST /api/rebase/squash` — squash commits. Request body: `{ "commit_hashes": ["<hash>", ...], "select_index": <int> }`
- `POST /api/rebase/fixup` — fixup commit into parent. Request body: `{ "commit_hashes": ["<hash>", ...], "select_index": <int> }`
- `POST /api/rebase/reword` — rename commit message. Request body: `{ "commit_hashes": ["<hash>"], "new_message": "...", "select_index": <int> }`
- `POST /api/rebase/split` — split commit. Request body: `{ "commit_hash": "<hash>", "files_to_split": ["<path>", ...] }`

  `select_index` (move/squash/fixup/reword, optional) is the index into the post-operation `commits` list whose diff the server bundles into the response `diff` field (see State object shape). The client sends an index rather than a hash because the resulting hash does not exist until the operation runs. Omitted or out of range → `diff` stays `null`.
- `POST /api/branch` — create branch. Request body: `{ "branch_name": "<name>", "commit_hash": "<hash>" }`.
- `DELETE /api/branch` — delete branch. Request body: `{ "branch_name": "<name>" }`.
- `POST /api/rebase/continue` — runs `git rebase --continue`.
- `POST /api/rebase/abort` — runs `git rebase --abort`.
- `POST /api/reset` — hard reset. Request body: `{ "commit_hash": "<hash>" }`.
- `POST /api/switch` — switch branch. Request body: `{ "branch": "<name>", "allow_different_gitmodules": <bool> }` (allow_different_gitmodules optional, default false).
- `POST /api/submodule/update` — runs `git submodule update --init`.
- `POST /api/quit` — shuts down server.
- `GET /api/show?commit_hash=<hash>` — returns commit details and diff.

### Response format

All responses are JSON. Mutations return the state object. Errors return:
```json
{
  "ok": false,
  "error": "dirty_tree" | "no_stash" | "nothing_to_stash" | "invalid_request" | "git_failed" | "gitmodules_differ" | "gitmodules_in_range" | "invalid_branch" | "rebase_in_progress" | "detached_head" | "invalid_commit" | "not_in_rebase" | "history_changed" | "cannot_delete_current_branch" | "split_failed"
}
```

Conflicts return full state with `"ok": false, "conflict": true, "conflict_files": [...]`.

Token auth failures return HTTP 403 with empty body. Unhandled exceptions return Flask's default 500.

## State object shape

```json
{
  "ok": true,
  "branch": "main",
  "upstream": "origin/main",
  "branches": ["main", "feature-x"],
  "dirty": false,
  "has_stash": false,
  "rebase_in_progress": false,
  "conflict": false,
  "conflict_files": [],
  "reflog_expiry": "30 days",
  "commits": [
    {
      "commit_hash": "a1b2c3d4e5f6...",
      "short_hash": "a1b2c3d",
      "message": "Fix login bug",
      "author": "Jane Smith",
      "date": "2026-04-01",
      "branches": ["main"],
      "tags": ["v1.2.0"],
      "is_head": true,
      "pushed": false
    }
  ],
  "undo_stack": [
    {
      "commit_hash": "a1b2c3d...",
      "label": "commit: Fix login bug",
      "timestamp": "2026-04-06T10:00:00"
    }
  ],
  "diff": null
}
```

Fields:
- `branch` — currently checked-out branch.
- `upstream` — the branch's upstream (e.g. `origin/main`), or `""` if none; drives the `pushed` flag on commits.
- `branches` — all local branches.
- `commits` — all visible commits, newest first. When the index has staged changes, a synthetic first entry with `commit_hash`/`short_hash` `"(Staged)"` represents them; `GET /api/show?commit_hash=%28Staged%29` returns the staged diff (`git diff --cached`).
- `undo_stack` — branch operations from reflog, newest first, deduped to oldest occurrence of each hash. Persists for `gc.reflogExpireUnreachable` (default 30 days).
- `dirty` — working tree has uncommitted changes.
- `has_stash` — stashed changes exist.
- `rebase_in_progress` — `.git/rebase-merge` or `.git/rebase-apply` exists.
- `conflict` — rebase has conflicts.
- `conflict_files` — files with conflicts.
- `reflog_expiry` — git config value shown in Undo Stack footer.
- `submodule_update_suggested` (optional) — reset changed gitlinks, offer `git submodule update --init`.
- `diff` — `null` normally. When a move/squash/fixup/reword request supplies `select_index` (see REST API), the server bundles the post-operation selected commit's show response (`{ "commit": {...}, "diff": "...", "files": [...] }`) here so the diff pane refreshes in one round-trip.

## Safeguards

**Undo Stack Expiration:** sourced from reflog, persists for `gc.reflogExpireUnreachable` (default 30 days, configurable).

**Submodule Guards:** prevent `.gitmodules` / gitlink corruption:
- **move**: refuse with `gitmodules_in_range` if any moved commit touches `.gitmodules`.
- **reset**: refuse with `gitmodules_differ` if `.gitmodules` content differs. If same content but gitlink set differs, allow and return `submodule_update_suggested: true`.
- **switch**: warn with `gitmodules_differ` if `.gitmodules` content differs; the UI confirms and re-sends with `allow_different_gitmodules: true` to proceed (unlike `reset`, which refuses).

## UI Layout

Two-column layout: Commit History (left) and Undo Stack (right).

Commit row elements (left to right): drag handle `⠿`, short hash, ref badges (blue=branch, green=tag), message, author, date. Row-hover action: fixup (image button). States: default, selected (blue), dragging, editing.

Toolbar: logo, branch switcher, action buttons. Status banner below (dirty tree, errors). Spinner overlay while request in flight.

## UI Interactions

**Selection:** Click to select (set anchor), Shift-click to extend contiguously. Ctrl/Cmd-click not supported. Escape cancels drag. One commit always selected; HEAD selected after operations. Clicking a row shows its diff.

**Move (drag):** Mousedown on `⠿` lifts selection with drop-line. Mouseup sends `POST /api/rebase/move` with new hash order. On conflict, opens dialog.

**Squash:** Floating action bar when ≥2 consecutive selected. Sends `POST /api/rebase/squash` with hashes.

**Fixup:** Row-hover image button (disabled on root). Sends fixup operation.

**Reword:** Right-click commit row → context-menu item "Edit commit message" → inline edit. Enter/blur-with-changes sends reword. Escape/blur-without-changes cancels.

**Split:** Select a single commit; the diff pane lists its files. Click files to choose a strict, non-empty subset (Shift-click extends a range). The "Split Commit" button (in the diff pane) sends `POST /api/rebase/split` with `commit_hash` and `files_to_split`. The button is shown only for a single non-staged commit with ≥2 files, and is enabled only while a strict subset is selected. The commit is replaced by two: the unselected files, then the selected files, both reusing its author and message.

**Branch switcher:** `<select>` dropdown. Sends `POST /api/switch`. Disabled if dirty or rebase in progress.

**Undo Stack:** Vertical list, newest first. Right-click entry → context-menu item "Reset branch to here" → `POST /api/reset` to that hash. Current HEAD has accent border and badge. Disabled if dirty or rebase in progress.

**Undo/Redo:** Navigate the Undo Stack by one step. Disabled when at oldest/newest or if dirty/rebasing.

**Conflict dialog:** Modal shown when `rebase_in_progress` is true. Cancel → `POST /api/rebase/abort`. Continue → `POST /api/rebase/continue`. Blocks other UI. Survives tab reload.

**Stash button:** Visible in dirty-tree banner. Sends `POST /api/stash`.

**Stash pop button:** Visible when stash exists and tree clean. Sends `POST /api/stash/pop`.

**Refresh button:** Top-right, always visible. Sends `GET /api/state`. Also fires on `window.focus`.

**Submodule update button:** Modal confirm shown after reset/switch when `submodule_update_suggested` true. Sends `POST /api/submodule/update`.

**Diff pane:** Resizable panel at bottom. Shows selected commit's diff with files left, unified diff right (green=added, red=deleted). Refreshes after every successful operation. The file list also hosts the "Split Commit" button (see **Split**).

**Quit button:** Sends `POST /api/quit` then displays "Server stopped" message.

**Error handling:** Non-conflict errors show dismissible red banner. Dirty-tree errors show persistent yellow banner with Stash button.

## Logging

After every successful mutating operation, append one line to `~/.git-history.log`:
```
<iso-timestamp> <branch> <full-hash>
```
Append-only, shared across all repos/branches. Served as plain text via `/log` link (no token required).

## Implementation notes

1. Full hashes internally; short hashes for display only.
2. Todo/message temp files: UTF-8, `newline="\n"` (git rejects CRLF on Windows).
3. Editor env-vars: use `shlex.quote` for paths (git's bundled bash parses correctly).
4. Temp files: created via `tempfile.mkstemp`, deleted in `finally` block.
5. Dirty-tree check: `git status --porcelain --untracked-files=no`; non-empty = dirty.
6. Conflict file list: `git diff --name-only --diff-filter=U` when `.git/rebase-merge` exists.
7. Token auth: `X-Token` mismatch → HTTP 403, empty body.
8. Detached HEAD: rejected at startup via `git symbolic-ref --quiet HEAD`.
9. Git log format: `--format=%H%x1f%h%x1f%an%x1f%ai%x1f%B%x1f%D%x00` (unit-sep, null-terminated). `%B` for full message (reword round-trips). `%ai` ISO 8601; UI shows first 10 chars.
10. Multiple instances: each gets own port/token; `.git/index.lock` prevents corruption.

## Installation

```
pip install git+https://github.com/BjarneH/git-history
```

Then run `git-history` from any git repository.
