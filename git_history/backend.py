import datetime
from dataclasses import dataclass
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


_LOG_PATH = Path.home() / ".git-history.log"

# Limit visible commits to this many to avoid overwhelming the UI and slow performance on large repos.
_HISTORY_DEPTH = 200

_EDITOR_PY = str(Path(__file__).parent / "editor.py")

_INDEX_HASH = "index"


@dataclass
class Commit:
    commit_hash: str
    short_hash: str
    message: str
    author: str
    date: str
    branches: list
    tags: list
    is_head: bool = False
    pushed: bool = False


@dataclass
class BranchHistoryEntry:
    commit_hash: str
    label: str
    timestamp: str


@dataclass
class StateResponse:
    branch: str
    branches: list
    dirty: bool
    has_stash: bool
    rebase_in_progress: bool
    conflict_files: list
    commits: list
    branch_history: list
    ok: bool = True
    submodule_update_suggested: bool = False
    conflict: bool = False
    reflog_expiry: str = ""


@dataclass
class ErrorResponse:
    error: str
    message: str = ""
    ok: bool = False


@dataclass
class ShowResponse:
    commit: Commit
    diff: str
    ok: bool = True


@dataclass
class _RebaseInstructions:
    todo_lines: list
    base: str = None       # None means --root
    msg_path: str = None   # temp file to unlink; reword only
    extends_base: bool = False
    hashes: list = None    # hashes involved in the operation (for fold operations)
    operation_label: str = None  # pre-computed branch history label for fold operations
    visible_commits: list = None  # cached commit list to avoid re-fetching in _rebase()


class GitHistory:
    """All git operations for a single repository.

    The public API is the contract between this module and ``rest_api.py``
    (Flask routes) / ``__init__.py`` (CLI ``--undo``/``--redo``). Methods
    return a dataclass (``StateResponse``, ``ShowResponse``) or
    ``ErrorResponse`` on a known failure; unhandled exceptions propagate.

    State / read-only:
        ``read_state(submodule_update_suggested=False)`` — full UI state.
        ``show(commit_hash)`` — commit details and diff.
        ``read_log()`` — contents of ``~/.git-history.log`` as text.

    Working tree:
        ``stash()`` / ``stash_pop()`` — wrap ``git stash push`` / ``pop``.
        ``submodule_update()`` — ``git submodule update --init``.
        ``switch_branch(name)`` — ``git switch``.
        ``reset(commit_hash)`` — hard reset to a hash visible in branch
        history; guards ``.gitmodules`` (see Submodule Guards).

    History rewrite (all go through ``_rebase`` — see the rebase section
    below for the per-operation todo construction):
        ``move(commits_newest_first)`` — reorder commits.
        ``squash(hashes)`` — fold a contiguous selection into the commit
        before it.
        ``fixup(hash)`` — fold a single commit into its parent, keeping
        the parent's message.
        ``reword(hash, message)`` — replace a commit message.
        ``rebase_continue()`` / ``rebase_abort()`` — drive a paused rebase.

    Caching: per-commit-hash caches (``_commit_cache``, ``_subject_cache``,
    ``_gitmodules_cache``, ``_gitlinks_cache``,
    ``_commit_touches_gitmodules_cache``) speed up the second
    ``read_state`` call. Keys are full 40-char commit hashes, which are
    immutable content addresses, so the caches do not need invalidation.

    Submodule guards prevent ``.gitmodules`` / gitlink corruption:
      * ``move`` refuses with ``gitmodules_in_range`` if any moved commit
        touches ``.gitmodules``.
      * ``reset`` refuses with ``gitmodules_differ`` if ``.gitmodules``
        content differs between HEAD and the target. If the content is the
        same but the gitlink set differs, the reset is allowed and the
        returned state has ``submodule_update_suggested=True`` so the UI
        can offer a ``git submodule update --init`` button.
    """

    def __init__(self, repo_path, log_path=_LOG_PATH):
        self.repo = Path(repo_path)
        self._log_path = Path(log_path)
        self._git_dir = None
        self._reflog_expiry = None
        self._commit_cache = {}
        self._subject_cache = {}
        self._resolve_cache = {}
        self._gitmodules_cache = {}
        self._gitlinks_cache = {}
        self._commit_touches_gitmodules_cache = {}
        self._start = self._resolve_commit(f"HEAD~{_HISTORY_DEPTH}")

    # ------------------------------------------------------------------
    # low-level helpers
    # ------------------------------------------------------------------

    def _run(self, args, env=None):
        return subprocess.run(
            args, cwd=str(self.repo), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )

    def _git_err(self, r):
        return ErrorResponse(error="git_failed", message=r.stderr.strip())

    def _resolve_commit(self, ref):
        if ref is None:
            return None
        if ref in self._resolve_cache:
            return self._resolve_cache[ref]
        r = self._run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"])
        if r.returncode == 0:
            result = r.stdout.strip()
            if len(ref) == 40:  # only cache full hashes — symbolic refs (HEAD, ORIG_HEAD, base^, …) change across mutations
                self._resolve_cache[ref] = result
            return result
        return None

    def _current_branch(self):
        r = self._run(["git", "symbolic-ref", "--short", "HEAD"])
        return r.stdout.strip() if r.returncode == 0 else ""

    def _list_local_branches(self):
        r = self._run(["git", "branch", "--format=%(refname:short)"])
        return [b.strip() for b in r.stdout.splitlines() if b.strip()]

    def _is_dirty(self):
        r = self._run(["git", "status", "--porcelain", "--untracked-files=no"])
        return bool(r.stdout.strip())

    def _has_stash(self):
        r = self._run(["git", "stash", "list"])
        return bool(r.stdout.strip())

    def _append_log(self):
        branch = self._current_branch()
        head = self._resolve_commit("HEAD")
        if branch and head:
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            try:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(f"{ts} {branch} {head}\n")
            except OSError as e:
                print(f"warning: failed to append to log at {self._log_path}: {e}", file=sys.stderr)

    def _git_dir_path(self):
        if self._git_dir is None:
            r = self._run(["git", "rev-parse", "--git-dir"])
            if r.returncode != 0:
                return None
            p = Path(r.stdout.strip())
            self._git_dir = p if p.is_absolute() else (self.repo / p).resolve()
        return self._git_dir

    def _in_rebase(self):
        git_dir = self._git_dir_path()
        if git_dir is None:
            return False
        return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

    def _conflict_files(self):
        r = self._run(["git", "diff", "--name-only", "--diff-filter=U"])
        return [ln for ln in r.stdout.splitlines() if ln]

    def _get_gitmodules(self, commit_hash):
        if commit_hash in self._gitmodules_cache:
            return self._gitmodules_cache[commit_hash]
        r = self._run(["git", "show", f"{commit_hash}:.gitmodules"])
        result = r.stdout if r.returncode == 0 else ""
        self._gitmodules_cache[commit_hash] = result
        return result

    def _gitlinks_at(self, commit_hash):
        if commit_hash in self._gitlinks_cache:
            return self._gitlinks_cache[commit_hash]
        r = self._run(["git", "ls-tree", "-r", commit_hash])
        result = {}
        for line in r.stdout.splitlines():
            if not line.startswith("160000 "):
                continue
            tab = line.find("\t")
            if tab != -1:
                parts = line[:tab].split()
                if len(parts) >= 3:
                    result[line[tab + 1:]] = parts[2]
        self._gitlinks_cache[commit_hash] = result
        return result

    def _commit_touches_gitmodules(self, commit_hash):
        if commit_hash in self._commit_touches_gitmodules_cache:
            return self._commit_touches_gitmodules_cache[commit_hash]
        r = self._run(["git", "diff-tree", "--no-commit-id", "-r", "--name-only", commit_hash])
        result = ".gitmodules" in r.stdout.splitlines()
        self._commit_touches_gitmodules_cache[commit_hash] = result
        return result

    def _any_moved_commit_touches_gitmodules(self, current_order, new_order):
        moved = [h for h, h2 in zip(current_order, new_order) if h != h2]
        return any(self._commit_touches_gitmodules(h) for h in moved)

    def _conflict_response(self):
        state = self.read_state()
        state.ok = False
        state.conflict = True
        return state

    def _get_reflog_expiry_timespan(self):
        if self._reflog_expiry is None:
            r = self._run(["git", "config", "gc.reflogExpireUnreachable"])
            self._reflog_expiry = r.stdout.strip().replace('.', ' ') if r.returncode == 0 else "30 days"
        return self._reflog_expiry

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    def read_state(self, submodule_update_suggested=False):
        branch = self._current_branch()
        commits = self._list_commits()
        pushed = self._get_pushed_hashes(commits, branch)
        for c in commits:
            c.pushed = c.commit_hash in pushed
        if self._run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
            staged = Commit(commit_hash=_INDEX_HASH, short_hash=_INDEX_HASH, message="(Staged changes)",
                           author="", date="", branches=[_INDEX_HASH], tags=[])
            commits.insert(0, staged)
        return StateResponse(
            branch=branch,
            branches=self._list_local_branches(),
            dirty=self._is_dirty(),
            has_stash=self._has_stash(),
            rebase_in_progress=self._in_rebase(),
            conflict_files=self._conflict_files(),
            commits=commits,
            branch_history=self._list_branch_history(branch),
            submodule_update_suggested=submodule_update_suggested,
            reflog_expiry=self._get_reflog_expiry_timespan(),
        )

    def _get_pushed_hashes(self, commits, branch):
        if not branch:
            return set()
        r_upstream = self._run(["git", "rev-parse", "--symbolic-full-name", "@{upstream}"])
        if r_upstream.returncode != 0:
            return set()
        remote_ref = r_upstream.stdout.strip()
        r = self._run(["git", "log", "--format=%H", f"{remote_ref}..HEAD"])
        if r.returncode != 0:
            return set()
        unpushed = set(r.stdout.splitlines())
        return {c.commit_hash for c in commits} - unpushed

    def _list_commits(self):
        fmt = "%H%x1f%h%x1f%an%x1f%ai%x1f%B%x1f%D%x00"
        args = ["git", "log", "--abbrev=7", f"--format={fmt}"]
        if self._start:
            args.append(f"{self._start}..HEAD")
        else:
            args.append("HEAD")
        r = self._run(args)
        if r.returncode != 0:
            return []

        commits = []
        for record in r.stdout.split("\x00"):
            record = record.lstrip("\n")
            if not record:
                continue
            parts = record.split("\x1f")
            if len(parts) < 6:
                continue
            h, sh, author, date, body, refs = parts[:6]
            branches, tags = self._parse_refs(refs)
            message = body.rstrip("\n")
            self._subject_cache[h] = message.split("\n", 1)[0].rstrip()
            commits.append(Commit(
                commit_hash=h,
                short_hash=sh[:7],
                message=message,
                author=author,
                date=date,
                branches=branches,
                tags=tags,
            ))
        if commits:
            commits[0].is_head = True
        return commits

    @staticmethod
    def _parse_refs(refs):
        branches, tags = [], []
        for part in (p.strip() for p in refs.split(",")):
            if not part:
                continue
            if part.startswith("HEAD -> "):
                branches.append(part[len("HEAD -> "):])
            elif part == "HEAD":
                continue
            elif part.startswith("tag: "):
                tags.append(part[len("tag: "):])
            else:
                branches.append(part)
        return branches, tags

    @staticmethod
    def _describe_rebase_group(labels):
        """Return a label from HEAD reflog subject strings between rebase (finish) and rebase (start)."""
        fixup_msgs, squash_msgs, reword_msgs = [], [], []
        for lbl in labels:
            if lbl.startswith("rebase (fixup): "):
                fixup_msgs.append(lbl[len("rebase (fixup): "):])
            elif lbl.startswith("rebase (squash): "):
                squash_msgs.append(lbl[len("rebase (squash): "):])
            elif lbl.startswith("commit (amend): "):
                reword_msgs.append(lbl[len("commit (amend): "):])
            elif lbl.startswith("rebase (reword): "):
                reword_msgs.append(lbl[len("rebase (reword): "):])
        op_count = sum(bool(msgs) for msgs in [fixup_msgs, squash_msgs, reword_msgs])
        if op_count > 1:
            return "rebase"
        if fixup_msgs:
            return "fixup: " + "; ".join(fixup_msgs)
        if squash_msgs:
            return "squash: " + "; ".join(squash_msgs)
        if reword_msgs:
            return "reword: " + reword_msgs[0]
        pick_count = sum(1 for lbl in labels if lbl.startswith("rebase (pick)"))
        if pick_count:
            return f"reorder: HEAD~{pick_count}"
        return "rebase"

    def _build_head_rebase_labels(self):
        """Read HEAD reflog and return {finish_hash: label} for each completed rebase."""
        r = self._run(["git", "reflog", "HEAD", "--format=%H%x1f%gs"])
        if r.returncode != 0:
            return {}
        labels = {}
        finish_hash = None
        group = []
        for line in r.stdout.splitlines():
            if not line:
                continue
            parts = line.split("\x1f", 1)
            if len(parts) < 2:
                continue
            h, gs = parts
            if gs.startswith("rebase (finish)"):
                finish_hash = h
                group = []
            elif gs.startswith("rebase (start)"):
                if finish_hash:
                    labels[finish_hash] = GitHistory._describe_rebase_group(group)
                    finish_hash = None
                    group = []
            elif finish_hash is not None:
                group.append(gs)
        return labels

    @staticmethod
    def _filter_rebase_groups(entries, rebase_labels=None):
        """Keep only rebase (finish) from rebase groups; filter all other rebase entries."""
        result = []
        for e in entries:
            if e.label.startswith("rebase (finish)"):
                label = (rebase_labels or {}).get(e.commit_hash, "rebase")
                result.append(BranchHistoryEntry(commit_hash=e.commit_hash, label=label, timestamp=e.timestamp))
            elif not e.label.startswith("rebase"):
                result.append(e)
        return result

    def _enhance_reset_labels(self, entries):
        """Look up commit message for entries starting with 'reset' and label them 'reset: <subject>'."""
        result = []
        for e in entries:
            if e.label.startswith("reset"):
                subject = self._subject_cache.get(e.commit_hash)
                if subject:
                    e = BranchHistoryEntry(commit_hash=e.commit_hash, label=f"reset: {subject}", timestamp=e.timestamp)
            result.append(e)
        return result

    def _list_branch_history(self, branch=None):
        if branch is None:
            branch = self._current_branch()
        if not branch:
            return []
        r = self._run(["git", "reflog", f"refs/heads/{branch}", "--format=%H%x1f%gs%x1f%ci"])
        if r.returncode != 0:
            return []
        raw = []
        for line in r.stdout.splitlines():
            if not line:
                continue
            parts = line.split("\x1f")
            if len(parts) < 3:
                continue
            h, label, ts = parts
            raw.append(BranchHistoryEntry(commit_hash=h, label=label, timestamp=ts))
        # Deduplicate by hash keeping the oldest entry (last in newest-first list).
        # This ensures reset/rebase intermediates are discarded in favour of the
        # original commit label, keeping the displayed list stable after undo/redo.
        rebase_labels = self._build_head_rebase_labels()
        filtered = self._filter_rebase_groups(raw, rebase_labels)
        seen = set()
        oldest_first = []
        for e in reversed(filtered):
            if e.commit_hash not in seen:
                seen.add(e.commit_hash)
                oldest_first.append(e)
        result = list(reversed(oldest_first))
        # Cache messages for all branch history commits to avoid individual lookups
        uncached = [e.commit_hash for e in result if e.commit_hash not in self._subject_cache]
        if uncached:
            fmt = "%H%x1f%s%x00"
            for i in range(0, len(uncached), 500):
                chunk = uncached[i:i + 500]
                r = self._run(["git", "log", f"--format={fmt}", "--no-walk", "--ignore-missing"] + chunk)
                if r.returncode != 0:
                    continue
                for record in r.stdout.split("\x00"):
                    record = record.strip()
                    if record:
                        parts = record.split("\x1f", 1)
                        if len(parts) == 2:
                            self._subject_cache[parts[0]] = parts[1].rstrip()
        return self._enhance_reset_labels(result)

    # ------------------------------------------------------------------
    # stash
    # ------------------------------------------------------------------

    def stash(self):
        if self._in_rebase():
            return ErrorResponse(error="rebase_in_progress")
        if not self._is_dirty():
            return ErrorResponse(error="nothing_to_stash")
        r = self._run(["git", "stash", "push"])
        return self._git_err(r) if r.returncode != 0 else self.read_state()

    def stash_pop(self):
        if self._in_rebase():
            return ErrorResponse(error="rebase_in_progress")
        if not self._has_stash():
            return ErrorResponse(error="no_stash")
        if self._is_dirty():
            return ErrorResponse(error="dirty_tree")
        r = self._run(["git", "stash", "pop"])
        return self._git_err(r) if r.returncode != 0 else self.read_state()

    # ------------------------------------------------------------------
    # reset
    # ------------------------------------------------------------------

    def reset(self, commit_hash):
        if self._in_rebase():
            return ErrorResponse(error="rebase_in_progress")
        resolved_hash = self._resolve_commit(commit_hash)
        if resolved_hash is None:
            return ErrorResponse(error="invalid_commit")
        head_hash = self._resolve_commit("HEAD")
        if head_hash and self._get_gitmodules(head_hash) != self._get_gitmodules(resolved_hash):
            return ErrorResponse(error="gitmodules_differ")
        if self._is_dirty():
            return ErrorResponse(error="dirty_tree")
        before_links = self._gitlinks_at(head_hash) if head_hash else {}
        r = self._run(["git", "reset", "--hard", resolved_hash])
        if r.returncode != 0:
            return self._git_err(r)
        self._append_log()
        return self.read_state(submodule_update_suggested=before_links != self._gitlinks_at(resolved_hash))

    def submodule_update(self):
        if self._in_rebase():
            return ErrorResponse(error="rebase_in_progress")
        r = self._run(["git", "submodule", "update", "--init"])
        return self._git_err(r) if r.returncode != 0 else self.read_state()

    def switch_branch(self, branch):
        if self._in_rebase():
            return ErrorResponse(error="rebase_in_progress")
        if self._is_dirty():
            return ErrorResponse(error="dirty_tree")
        if branch not in self._list_local_branches():
            return ErrorResponse(error="invalid_branch")
        before_links = self._gitlinks_at(self._resolve_commit("HEAD"))
        r = self._run(["git", "switch", branch])
        if r.returncode != 0:
            return self._git_err(r)
        self._start = self._resolve_commit(f"HEAD~{_HISTORY_DEPTH}")
        return self.read_state(submodule_update_suggested=before_links != self._gitlinks_at(self._resolve_commit("HEAD")))

    # ------------------------------------------------------------------
    # show
    # ------------------------------------------------------------------

    def show(self, commit_hash):
        if commit_hash == _INDEX_HASH:
            r = self._run(["git", "diff", "--cached"])
            if r.returncode != 0:
                return self._git_err(r)
            return ShowResponse(commit=Commit(commit_hash=_INDEX_HASH, short_hash=_INDEX_HASH,
                                              message="", author="", date="", branches=[], tags=[]),
                               diff=r.stdout)
        resolved = self._resolve_commit(commit_hash)
        if resolved is None:
            return ErrorResponse(error="invalid_commit")
        if resolved in self._commit_cache:
            return self._commit_cache[resolved]
        fmt = "%H%x00%h%x00%an%x00%ai%x00%B%x00%D"
        log_r = self._run(["git", "log", "--abbrev=7", f"--format={fmt}", "-1", resolved])
        if log_r.returncode != 0:
            return self._git_err(log_r)
        parts = log_r.stdout.split("\x00")
        if len(parts) < 6:
            return ErrorResponse(error="git_failed")
        h, sh, author, date, body, refs = parts[:6]
        branches, tags = self._parse_refs(refs)
        diff_r = self._run(["git", "show", "--format=", resolved])
        if diff_r.returncode != 0:
            return self._git_err(diff_r)
        result = ShowResponse(commit=Commit(commit_hash=h, short_hash=sh[:7], message=body.rstrip("\n"),
                                           author=author, date=date, branches=branches, tags=tags),
                             diff=diff_r.stdout)
        self._commit_cache[resolved] = result
        return result

    def read_log(self) -> str:
        try:
            return self._log_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    # ------------------------------------------------------------------
    # rebase
    #
    # All four mutating operations (move, squash, fixup, reword) build a
    # ``_RebaseInstructions`` and hand it to ``_rebase``, which writes the
    # todo file, runs ``git rebase -i --keep-empty --empty=keep``, and
    # cleans up. Commits are processed in the order git wants in the todo:
    # oldest first, even though the rest of this module is newest-first.
    #
    #   move: the UI sends the desired ordering of visible commits,
    #     newest-first. After reversing to oldest-first, the longest shared
    #     prefix with the current order is identified; everything before
    #     that prefix is untouched. Generate ``pick`` lines for the rebase
    #     range. Refuses with ``gitmodules_in_range`` if any moved commit
    #     touches ``.gitmodules``.
    #
    #   squash: the selected hashes must form a contiguous block in the
    #     commit list. Each selected commit is marked ``squash`` in the
    #     todo so it folds into the commit directly before it; non-
    #     selected commits keep their ``pick``. Git's default squash
    #     concatenates messages automatically.
    #
    #   fixup: a single selected hash H. If H is the root commit, refuse
    #     with ``invalid_request``. Otherwise the todo includes H's parent
    #     as ``pick`` and H as ``fixup``. The rebase base is extended if
    #     needed so H's parent is in the todo.
    #
    #   reword: a single selected hash H. The new message is written to a
    #     temp file. The todo lists H as ``pick`` followed by
    #     ``exec git commit --amend --allow-empty -F <msg_path>``;
    #     everything else is ``pick``. This avoids relying on
    #     ``GIT_EDITOR`` for the message — the exec line applies it
    #     directly. The exec step always runs within ``_run`` (a pick
    #     replaying onto its own parent cannot conflict), so it is safe to
    #     unlink the message temp file in the ``finally`` block of
    #     ``_rebase``.
    # ------------------------------------------------------------------

    def move(self, commits_newest_first):
        instr = self._move_instructions(commits_newest_first)
        if isinstance(instr, ErrorResponse):
            return instr
        if not instr.todo_lines:
            return self.read_state()
        return self._rebase(instr)

    def squash(self, hashes):
        instr = self._squash_instructions(hashes)
        return self._rebase(instr)

    def fixup(self, hashes):
        instr = self._fixup_instructions(hashes)
        return self._rebase(instr)

    def reword(self, commit_hash, message):
        instr = self._reword_instructions(commit_hash, message)
        return self._rebase(instr)

    def _rebase(self, instr):
        if isinstance(instr, ErrorResponse):
            return instr
        if self._is_dirty():
            return ErrorResponse(error="dirty_tree")
        if self._in_rebase():
            return ErrorResponse(error="invalid_request")

        visible_commits = instr.visible_commits if instr.visible_commits is not None else self._list_commits()
        visible = [c.commit_hash for c in visible_commits]
        if not visible:
            return ErrorResponse(error="invalid_request")

        def unlink_safe(p):
            if p:
                try:
                    os.unlink(p)
                except OSError as e:
                    print(f"warning: failed to delete temp file {p}: {e}", file=sys.stderr)

        todo_path = None
        try:
            todo_path = self._write_tempfile("\n".join(instr.todo_lines) + "\n")
            env = self._rebase_env(todo_path=todo_path, msg_path=instr.msg_path)
            cmd = ["git", "rebase", "-i", "--keep-empty", "--empty=keep"]
            cmd.append("--root" if instr.base is None else instr.base)
            r = self._run(cmd, env=env)
        finally:
            # Safe to delete both here: reword cannot conflict (pick replays
            # onto its own parent), so the exec step always runs within _run.
            unlink_safe(todo_path)
            unlink_safe(instr.msg_path if instr else None)

        if r.returncode != 0 and not self._in_rebase():
            return self._git_err(r)

        # Squashing commits whose combined diff is empty (e.g. a file that is
        # created then deleted) leaves git paused mid-rebase despite
        # --empty=keep. One --continue drives it to completion.
        if self._in_rebase():
            err = self._drive_continue()
            if err is not None:
                return err

        if instr.extends_base and self._start is not None:
            # Old _start and the oldest visible commit were folded together;
            # advance _start to the resulting commit so the visible range is
            # stable across subsequent operations.
            new_visible = len(visible) - sum(1 for h in instr.hashes if h in visible)
            new_start = self._resolve_commit(f"HEAD~{new_visible}")
            if new_start is None:
                return ErrorResponse(error="start_update_failed")
            self._start = new_start

        self._append_log()
        before_links = self._gitlinks_at(visible[0])
        after_links = self._gitlinks_at(self._resolve_commit("HEAD"))
        state = self.read_state(submodule_update_suggested=before_links != after_links)
        if instr.operation_label and state.branch_history:
            e = state.branch_history[0]
            state.branch_history[0] = BranchHistoryEntry(
                commit_hash=e.commit_hash, label=instr.operation_label, timestamp=e.timestamp)
        return state

    def rebase_continue(self):
        if not self._in_rebase():
            return ErrorResponse(error="not_in_rebase")
        err = self._drive_continue()
        if err is not None:
            return err
        self._append_log()
        orig_head = self._resolve_commit("ORIG_HEAD")
        before_links = self._gitlinks_at(orig_head) if orig_head else {}
        after_links = self._gitlinks_at(self._resolve_commit("HEAD"))
        return self.read_state(submodule_update_suggested=before_links != after_links)

    def rebase_abort(self):
        if not self._in_rebase():
            return ErrorResponse(ok=False, error="not_in_rebase")
        r = self._run(["git", "rebase", "--abort"])
        return self._git_err(r) if r.returncode != 0 else self.read_state()

    # ------------------------------------------------------------------
    # rebase helpers
    # ------------------------------------------------------------------

    def _move_instructions(self, commits_newest_first):
        visible_commits = self._list_commits()
        visible = [c.commit_hash for c in visible_commits]
        if commits_newest_first is None or sorted(commits_newest_first) != sorted(visible):
            return ErrorResponse(error="invalid_request")
        if self._any_moved_commit_touches_gitmodules(visible, commits_newest_first):
            return ErrorResponse(error="gitmodules_in_range")
        changed_idx = [i for i, (h, h2) in enumerate(zip(visible, commits_newest_first)) if h != h2]
        if not changed_idx:
            return _RebaseInstructions(todo_lines=[], base=None, visible_commits=visible_commits)
        oldest_changed = max(changed_idx)
        range_set = set(visible[:oldest_changed + 1])
        todo_hashes = self._to_interactive_rebase_order([h for h in commits_newest_first if h in range_set])
        base = visible[oldest_changed + 1] if oldest_changed + 1 < len(visible) else self._start
        return _RebaseInstructions(todo_lines=[f"pick {h}" for h in todo_hashes], base=base, visible_commits=visible_commits)

    def _squash_instructions(self, hashes):
        return self._fold_instructions(hashes, "squash")

    def _fixup_instructions(self, hashes):
        return self._fold_instructions(hashes, "fixup")

    def _fold_instructions(self, hashes, operation):
        visible_commits = self._list_commits()
        visible = [c.commit_hash for c in visible_commits]
        if not hashes or any(h not in visible for h in hashes):
            return ErrorResponse(error="invalid_request")
        indices = sorted(visible.index(h) for h in hashes)
        if indices != list(range(indices[0], indices[-1] + 1)):
            return ErrorResponse(error="invalid_request")
        if len(hashes) == 1:
            rebase_commands = {hashes[0]: operation}
        else:
            # The oldest in the group stays as pick; the rest fold into it.
            oldest_in_group = max(hashes, key=visible.index)
            rebase_commands = {h: operation for h in hashes if h != oldest_in_group}
        msg_by_hash = {c.commit_hash: c.message.split("\n")[0] for c in visible_commits}
        operation_label = operation + ": " + "; ".join(msg_by_hash[h] for h in rebase_commands)
        oldest_idx = indices[-1]
        todo_hashes = self._to_interactive_rebase_order(visible[:oldest_idx + 2])
        base = visible[oldest_idx + 2] if oldest_idx + 2 < len(visible) else self._start
        extends_base = visible[-1] in hashes
        if extends_base:
            if base is None:
                # Root commit is the squash target (nothing to fold into).
                if visible[-1] in rebase_commands:
                    return ErrorResponse(error="invalid_request")
            else:
                parent = self._resolve_commit(f"{base}^")
                if parent is None:
                    return ErrorResponse(error="invalid_request")
                todo_hashes = [base] + todo_hashes
                base = parent
        return _RebaseInstructions(
            todo_lines=[f"{rebase_commands.get(h, 'pick')} {h}" for h in todo_hashes],
            base=base,
            extends_base=extends_base,
            hashes=hashes,
            operation_label=operation_label,
            visible_commits=visible_commits,
        )

    def _reword_instructions(self, commit_hash, message):
        visible_commits = self._list_commits()
        visible = [c.commit_hash for c in visible_commits]
        if not (message and message.strip()) or commit_hash not in visible:
            return ErrorResponse(error="invalid_request")
        idx = visible.index(commit_hash)
        todo_hashes = self._to_interactive_rebase_order(visible[:idx + 1])
        base = visible[idx + 1] if idx + 1 < len(visible) else self._start
        msg_path = self._write_tempfile(message + "\n")
        todo_lines = []
        for h in todo_hashes:
            todo_lines.append(f"pick {h}")
            if h == commit_hash:
                todo_lines.append(f"exec git commit --amend --allow-empty -F {shlex.quote(msg_path)}")
        return _RebaseInstructions(todo_lines=todo_lines, base=base, msg_path=msg_path, visible_commits=visible_commits)

    def _drive_continue(self):
        if self._conflict_files():
            return self._conflict_response()
        env = self._rebase_env()
        r = self._run(["git", "rebase", "--continue"], env=env)
        if self._in_rebase():
            return self._conflict_response()
        if r.returncode != 0:
            return self._git_err(r)
        return None

    def _rebase_env(self, todo_path=None, msg_path=None):
        env = os.environ.copy()
        editor_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(_EDITOR_PY)}"
        env["GIT_SEQUENCE_EDITOR"] = editor_cmd
        env["GIT_EDITOR"] = editor_cmd
        if todo_path:
            env["GIT_HISTORY_TODO"] = todo_path
        if msg_path:
            env["GIT_HISTORY_MSG"] = msg_path
        return env

    @staticmethod
    def _write_tempfile(content):
        fd, path = tempfile.mkstemp(suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        except:
            os.unlink(path)
            raise
        return path

    @staticmethod
    def _to_interactive_rebase_order(commit_instructions_newest_first):
        # git rebase -i requires oldest-first; the rest of this module is newest-first
        return list(reversed(commit_instructions_newest_first))
