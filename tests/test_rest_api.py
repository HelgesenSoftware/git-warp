"""
Unit tests for the git-warp REST API.

These tests exercise the Flask endpoints defined in the plan. They verify
HTTP methods, status codes, JSON structure, auth token enforcement, and that
each endpoint correctly delegates to the GitWarp backend.

The Flask app is expected to live in ``git_warp.py`` and expose a
``create_app(repo_path, token)`` factory that returns a configured Flask app.

Endpoints under test:

    GET  /api/state                -> state JSON
    POST /api/stash                -> state | error JSON
    POST /api/stash/pop            -> state | error JSON
    POST /api/rebase/move          -> state | conflict | error JSON
    POST /api/rebase/squash        -> state | conflict | error JSON
    POST /api/rebase/fixup         -> state | conflict | error JSON
    POST /api/rebase/reword        -> state | conflict | error JSON
    POST /api/rebase/split         -> state | conflict | error JSON
    POST /api/rebase/continue      -> state | conflict | error JSON
    POST /api/rebase/abort         -> state | error JSON
    POST /api/reset                -> state | error JSON
    GET  /api/show?commit_hash=<commit_hash>  -> {ok, info, diff} | error JSON

Run with:
    python -m unittest tests.test_rest_api
"""
import os
from unittest.mock import patch
import pytest
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_test_repo import COMMITS, create_lib_repo, init_repo, make_commit
from conftest import _ensure_persistent_test_repo, _commit_raw
from git_warp.rest_api import create_app


TOKEN = "test-token-abc123"


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------

class StandardAPITest(unittest.TestCase):
    """Fresh clone of the 21-commit test repo with a Flask test client."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-api-test-"))
        cls.repo = cls.tmpdir / "repo"
        persistent_repo = _ensure_persistent_test_repo()
        # Set clone-time local config (-c) to avoid extra subprocess spawns:
        #   protocol.file.allow — file:// submodule operations (as persistent repo)
        #   user.name/email     — git clone does not copy local user config, and CI
        #                         runners have no global identity, so rebases that
        #                         create commits would otherwise fail.
        subprocess.run(["git", "clone",
                        "-c", "protocol.file.allow=always",
                        "-c", "user.name=Test User",
                        "-c", "user.email=test@example.com",
                        str(persistent_repo), str(cls.repo)],
                       capture_output=True, check=True)
        # Remove origin remote (clone sets it to persistent repo, but tests expect no remote)
        subprocess.run(["git", "remote", "remove", "origin"],
                       cwd=str(cls.repo), capture_output=True)
        # Ensure refs/heads/main reflog has at least one entry (not guaranteed after a fresh clone).
        subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=str(cls.repo), capture_output=True)
        # Store initial branch and HEAD for resetting between tests
        result = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                cwd=str(cls.repo), capture_output=True, text=True)
        cls.initial_branch = result.stdout.strip()
        result = subprocess.run(["git", "rev-parse", "HEAD"],
                                cwd=str(cls.repo), capture_output=True, text=True)
        cls.initial_head = result.stdout.strip()
        cls.app = create_app(str(cls.repo), TOKEN)
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        repo = str(self.__class__.repo)
        initial_head = self.__class__.initial_head
        initial_branch = self.__class__.initial_branch
        # Guard: skip full reset for tests that left the repo in a clean state
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                              capture_output=True, text=True).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain", "-b"], cwd=repo,
                                capture_output=True, text=True).stdout
        extra_branches = subprocess.run(
            ["git", "branch", "--list", "feature/my-feature", "bugfix/issue-123"],
            cwd=repo, capture_output=True, text=True).stdout.strip()
        # Exact branch-line match catches rebase-in-progress ("## HEAD (no branch)")
        # and added remotes ("## main...origin/main"), both differ from "## main"
        needs_reset = (head != initial_head
                       or status.strip() != f"## {initial_branch}"
                       or extra_branches)
        if needs_reset:
            subprocess.run(["git", "rebase", "--abort"], cwd=repo, capture_output=True)
            subprocess.run(["git", "remote", "remove", "origin"], cwd=repo, capture_output=True)
            subprocess.run(["git", "checkout", initial_branch], cwd=repo, capture_output=True)
            subprocess.run(["git", "reset", "--hard", initial_head],
                           cwd=repo, capture_output=True, check=True)
            subprocess.run(["git", "clean", "-fd"], cwd=repo, capture_output=True)
            for branch in ["feature/my-feature", "bugfix/issue-123"]:
                subprocess.run(["git", "branch", "-D", branch], cwd=repo, capture_output=True)
            subprocess.run(["git", "reflog", "expire", "--expire=now",
                           f"refs/heads/{initial_branch}"], cwd=repo, capture_output=True)
        # Clean up tmpdir subdirectories created by tests
        for item in self.__class__.tmpdir.iterdir():
            if item.name != "repo" and item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
        # Reference class-level resources for compatibility with existing test code
        self.tmpdir = self.__class__.tmpdir
        self.repo = self.__class__.repo
        self.app = self.__class__.app
        self.client = self.__class__.client

    def get(self, url, **kwargs):
        return self.client.get(url, headers={"X-Token": TOKEN}, **kwargs)

    def post(self, url, json=None, **kwargs):
        return self.client.post(url, json=json,
                                headers={"X-Token": TOKEN}, **kwargs)

    def delete(self, url, json=None, **kwargs):
        return self.client.delete(url, json=json,
                                  headers={"X-Token": TOKEN}, **kwargs)

    def make_dirty(self, path="README.md", content=b"# changed\n"):
        (self.repo / path).write_bytes(content)

    def make_staged(self, path="README.md", content=b"# staged\n"):
        (self.repo / path).write_bytes(content)
        subprocess.run(["git", "add", path], cwd=str(self.repo), check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@pytest.mark.release
class AuthTests(StandardAPITest):

    def test_missing_token_returns_403(self):
        resp = self.client.get("/api/state")
        self.assertEqual(resp.status_code, 403)

    def test_wrong_token_returns_403(self):
        resp = self.client.get("/api/state",
                               headers={"X-Token": "wrong"})
        self.assertEqual(resp.status_code, 403)

    def test_correct_token_returns_200(self):
        resp = self.get("/api/state")
        self.assertEqual(resp.status_code, 200)

    def test_auth_applies_to_post_endpoints(self):
        resp = self.client.post("/api/stash")
        self.assertEqual(resp.status_code, 403)

    def test_api_state_query_string_token_rejected(self):
        # Query-string token is not accepted; only the X-Token header authenticates.
        resp = self.client.get(f"/api/state?t={TOKEN}")
        self.assertEqual(resp.status_code, 403)

    def test_auth_not_required_for_root(self):
        # Static files / index don't require auth.  The root route may
        # return 404 if static files aren't present yet, but it must NOT
        # return 403.
        resp = self.client.get("/")
        self.assertNotEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# GET /api/state
# ---------------------------------------------------------------------------

class StateEndpointTests(StandardAPITest):

    @pytest.mark.release
    def test_returns_json(self):
        resp = self.get("/api/state")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content_type, "application/json")

    @pytest.mark.release
    def test_state_has_expected_fields(self):
        data = self.get("/api/state").get_json()
        self.assertTrue(data["ok"])
        self.assertIn("branch", data)
        self.assertIn("dirty", data)
        self.assertIn("has_stash", data)
        self.assertIn("rebase_in_progress", data)
        self.assertIn("conflict_files", data)
        self.assertIn("commits", data)
        self.assertIn("undo_stack", data)

    def test_state_commit_count(self):
        data = self.get("/api/state").get_json()
        self.assertEqual(len(data["commits"]), len(COMMITS) + 1)

    def test_state_commits_newest_first(self):
        data = self.get("/api/state").get_json()
        self.assertEqual(data["commits"][0]["message"], "Add CI workflow")
        self.assertEqual(data["commits"][-1]["message"], "Initial commit")

    @pytest.mark.release
    def test_head_commit_metadata(self):
        head = self.get("/api/state").get_json()["commits"][0]
        self.assertEqual(head["message"], "Add CI workflow")
        self.assertEqual(head["author"], "Bob Brown")
        self.assertTrue(head["is_head"])
        self.assertIn("main", head["branches"])
        self.assertEqual(head["tags"], [])

    def test_tags_appear_on_correct_commits(self):
        by_msg = {c["message"]: c for c in self.get("/api/state").get_json()["commits"]}
        self.assertIn("v0.1.0", by_msg["Add HTTP server module"]["tags"])
        self.assertIn("v0.2.0", by_msg["Add user model"]["tags"])
        self.assertIn("v1.0.0", by_msg["Add integration tests"]["tags"])

    @pytest.mark.release
    def test_short_hash_is_seven_char_prefix(self):
        for c in self.get("/api/state").get_json()["commits"]:
            self.assertEqual(len(c["short_hash"]), 7)
            self.assertTrue(c["commit_hash"].startswith(c["short_hash"]))

    def test_undo_stack_is_deduped_by_hash(self):
        hashes = [e["commit_hash"] for e in self.get("/api/state").get_json()["undo_stack"]]
        self.assertEqual(len(hashes), len(set(hashes)))

    @pytest.mark.release
    def test_undo_stack_entries_have_label_and_timestamp(self):
        for entry in self.get("/api/state").get_json()["undo_stack"]:
            self.assertIsNotNone(entry["commit_hash"])
            self.assertIsNotNone(entry["label"])
            self.assertIsNotNone(entry["timestamp"])

    def test_state_reports_dirty_tree(self):
        self.make_dirty()
        self.assertTrue(self.get("/api/state").get_json()["dirty"])

    @pytest.mark.release
    def test_state_includes_reflog_expiry_field(self):
        data = self.get("/api/state").get_json()
        self.assertIn("reflog_expiry", data)
        self.assertIsInstance(data["reflog_expiry"], str)
        self.assertGreater(len(data["reflog_expiry"]), 0)

    @pytest.mark.release
    def test_state_reflog_expiry_respects_git_config(self):
        # Set a custom reflog expiry
        subprocess.run(["git", "config", "gc.reflogExpireUnreachable", "30 days"],
                       cwd=str(self.repo), capture_output=True, check=True)
        data = self.get("/api/state").get_json()
        self.assertEqual(data["reflog_expiry"], "30 days")


# ---------------------------------------------------------------------------
# POST /api/stash
# ---------------------------------------------------------------------------

class StashEndpointTests(StandardAPITest):

    @pytest.mark.release
    def test_stash_clean_returns_nothing_to_stash(self):
        data = self.post("/api/stash").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "nothing_to_stash")

    def test_stash_dirty_succeeds(self):
        self.make_dirty()
        data = self.post("/api/stash").get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["dirty"])
        self.assertTrue(data["has_stash"])

    @pytest.mark.release
    def test_stash_uses_post(self):
        resp = self.get("/api/stash")
        self.assertEqual(resp.status_code, 405)


# ---------------------------------------------------------------------------
# POST /api/stash/pop
# ---------------------------------------------------------------------------

class StashPopEndpointTests(StandardAPITest):

    @pytest.mark.release
    def test_pop_no_stash_returns_error(self):
        data = self.post("/api/stash/pop").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "no_stash")

    @pytest.mark.release
    def test_pop_when_dirty_returns_error(self):
        self.make_dirty()
        self.post("/api/stash")
        self.make_dirty(path="LICENSE", content=b"dirty\n")
        data = self.post("/api/stash/pop").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")

    def test_pop_restores_dirty_state(self):
        self.make_dirty()
        self.post("/api/stash")
        data = self.post("/api/stash/pop").get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["dirty"])
        self.assertFalse(data["has_stash"])

    @pytest.mark.release
    def test_stash_pop_uses_post(self):
        resp = self.get("/api/stash/pop")
        self.assertEqual(resp.status_code, 405)


# ---------------------------------------------------------------------------
# POST /api/rebase/move
# ---------------------------------------------------------------------------

class RebaseMoveEndpointTests(StandardAPITest):

    def test_swap_two_newest(self):
        state = self.get("/api/state").get_json()
        commit_hashes = [c["commit_hash"] for c in state["commits"]]
        commit_hashes[0], commit_hashes[1] = commit_hashes[1], commit_hashes[0]

        data = self.post("/api/rebase/move", json={
            "order": commit_hashes,
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commits"][0]["message"],
                         state["commits"][1]["message"])

    def test_move_unchanged_order_is_noop(self):
        state = self.get("/api/state").get_json()
        commit_hashes = [c["commit_hash"] for c in state["commits"]]

        data = self.post("/api/rebase/move", json={
            "order": commit_hashes,
        }).get_json()
        self.assertTrue(data["ok"])
        new_hashes = [c["commit_hash"] for c in data["commits"]]
        self.assertEqual(new_hashes, commit_hashes)

    def test_move_distant_commit(self):
        state = self.get("/api/state").get_json()
        msgs_before = [c["message"] for c in state["commits"]]
        order = [c["commit_hash"] for c in state["commits"]]
        moved = order.pop(0)
        order.insert(5, moved)

        data = self.post("/api/rebase/move", json={ "order": order}).get_json()
        self.assertTrue(data["ok"])
        msgs_after = [c["message"] for c in data["commits"]]
        self.assertEqual(msgs_after[5], msgs_before[0])
        self.assertEqual(sorted(msgs_after), sorted(msgs_before))

    def test_move_multiple_selected_commits(self):
        state = self.get("/api/state").get_json()
        msgs_before = [c["message"] for c in state["commits"]]
        hashes = [c["commit_hash"] for c in state["commits"]]
        new_order = hashes[:5] + hashes[7:9] + hashes[5:7] + hashes[9:]

        data = self.post("/api/rebase/move", json={ "order": new_order}).get_json()
        self.assertTrue(data["ok"])
        msgs_after = [c["message"] for c in data["commits"]]
        self.assertGreater(msgs_after.index(msgs_before[5]), 6)
        self.assertGreater(msgs_after.index(msgs_before[6]), 6)
        self.assertEqual(sorted(msgs_after), sorted(msgs_before))

    @pytest.mark.release
    def test_move_refused_when_dirty(self):
        self.make_dirty()
        state = self.get("/api/state").get_json()
        commit_hashes = [c["commit_hash"] for c in state["commits"]]
        commit_hashes[0], commit_hashes[1] = commit_hashes[1], commit_hashes[0]

        data = self.post("/api/rebase/move", json={
            "order": commit_hashes,
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")

    @pytest.mark.release
    def test_rebase_uses_post(self):
        resp = self.get("/api/rebase/move")
        self.assertEqual(resp.status_code, 405)


# ---------------------------------------------------------------------------
# POST /api/rebase/squash
# ---------------------------------------------------------------------------

class RebaseSquashEndpointTests(StandardAPITest):

    def test_squash_two_adjacent(self):
        state = self.get("/api/state").get_json()
        commit_hashes = [state["commits"][0]["commit_hash"], state["commits"][1]["commit_hash"]]

        data = self.post("/api/rebase/squash", json={
            "commit_hashes": commit_hashes,
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["commits"]), len(state["commits"]) - 1)

    @pytest.mark.release
    def test_squash_refused_when_dirty(self):
        self.make_dirty()
        state = self.get("/api/state").get_json()
        commit_hashes = [state["commits"][0]["commit_hash"], state["commits"][1]["commit_hash"]]

        data = self.post("/api/rebase/squash", json={
            "commit_hashes": commit_hashes,
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")


# ---------------------------------------------------------------------------
# POST /api/rebase/fixup
# ---------------------------------------------------------------------------

class RebaseFixupEndpointTests(StandardAPITest):

    def test_fixup_middle_commit(self):
        state = self.get("/api/state").get_json()
        target = state["commits"][3]

        data = self.post("/api/rebase/fixup", json={
            "commit_hashes": [target["commit_hash"]],
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["commits"]), len(state["commits"]) - 1)
        msgs = [c["message"] for c in data["commits"]]
        self.assertNotIn(target["message"], msgs)

    @pytest.mark.release
    def test_fixup_root_refused(self):
        state = self.get("/api/state").get_json()
        root = state["commits"][-1]["commit_hash"]

        data = self.post("/api/rebase/fixup", json={
            "commit_hashes": [root],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    @pytest.mark.release
    def test_fixup_refused_when_dirty(self):
        self.make_dirty()
        h = self.get("/api/state").get_json()["commits"][3]["commit_hash"]
        data = self.post("/api/rebase/fixup", json={
            "commit_hashes": [h],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")


# ---------------------------------------------------------------------------
# POST /api/rebase/reword
# ---------------------------------------------------------------------------

class RebaseRewordEndpointTests(StandardAPITest):

    def test_reword_top_commit(self):
        state = self.get("/api/state").get_json()
        h = state["commits"][0]["commit_hash"]

        data = self.post("/api/rebase/reword", json={
            "commit_hashes": [h],
            "new_message": "Brand new message",
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commits"][0]["message"], "Brand new message")

    @pytest.mark.release
    def test_reword_missing_message_returns_error(self):
        state = self.get("/api/state").get_json()
        h = state["commits"][0]["commit_hash"]

        data = self.post("/api/rebase/reword", json={
            "commit_hashes": [h],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    @pytest.mark.release
    def test_reword_refused_when_dirty(self):
        self.make_dirty()
        state = self.get("/api/state").get_json()
        h = state["commits"][0]["commit_hash"]

        data = self.post("/api/rebase/reword", json={
            "commit_hashes":[h],
            "new_message": "x",
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")

    def test_reword_middle_commit(self):
        state = self.get("/api/state").get_json()
        h = state["commits"][5]["commit_hash"]
        data = self.post("/api/rebase/reword", json={
            "commit_hashes": [h], "new_message": "Reworded middle",
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commits"][5]["message"], "Reworded middle")
        self.assertEqual(data["commits"][0]["message"], state["commits"][0]["message"])
        self.assertEqual(data["commits"][-1]["message"], state["commits"][-1]["message"])

    @pytest.mark.release
    def test_reword_preserves_complex_message(self):
        h = self.get("/api/state").get_json()["commits"][0]["commit_hash"]
        new_msg = (
            'First line with "quotes" and \'single quotes\'\n\n'
            'Body with émojis 🎉, ñoñó chars, and C:\\path\\style'
        )
        data = self.post("/api/rebase/reword", json={
            "commit_hashes": [h], "new_message": new_msg,
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commits"][0]["message"], new_msg)


# ---------------------------------------------------------------------------
# Bundled diff: a mutation bundles the diff of the commit the UI will select
# (named by select_index) so the operation is a single round-trip.
# ---------------------------------------------------------------------------

class BundledDiffTests(StandardAPITest):

    def test_state_read_does_not_bundle_diff(self):
        # Plain reads carry no diff; it is bundled only for mutations.
        self.assertIsNone(self.get("/api/state").get_json()["diff"])

    def test_mutation_without_select_index_omits_diff(self):
        h = self.get("/api/state").get_json()["commits"][0]["commit_hash"]
        data = self.post("/api/rebase/reword", json={
            "commit_hashes": [h], "new_message": "No index",
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data["diff"])

    def test_reword_bundles_selected_commit_diff(self):
        # Reword a commit deep in history; the bundled diff is for that commit
        # (not HEAD), so the UI renders it without a second /api/show.
        h = self.get("/api/state").get_json()["commits"][5]["commit_hash"]
        data = self.post("/api/rebase/reword", json={
            "commit_hashes": [h], "new_message": "Reworded deep", "select_index": 5,
        }).get_json()
        self.assertTrue(data["ok"])
        selected = data["commits"][5]
        self.assertNotEqual(selected["commit_hash"], data["commits"][0]["commit_hash"])
        self.assertEqual(data["diff"]["commit"]["commit_hash"], selected["commit_hash"])

    def test_fixup_bundles_merge_result_diff(self):
        # The merge result lands at the fixup'd commit's index; its diff is bundled.
        target = self.get("/api/state").get_json()["commits"][3]
        data = self.post("/api/rebase/fixup", json={
            "commit_hashes": [target["commit_hash"]], "select_index": 3,
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["diff"]["commit"]["commit_hash"], data["commits"][3]["commit_hash"])

    def test_out_of_range_select_index_omits_diff(self):
        # Defensive bound: an index past the end bundles nothing rather than erroring.
        h = self.get("/api/state").get_json()["commits"][0]["commit_hash"]
        data = self.post("/api/rebase/reword", json={
            "commit_hashes": [h], "new_message": "Out of range", "select_index": 9999,
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data["diff"])


# ---------------------------------------------------------------------------
# POST /api/rebase/split
# ---------------------------------------------------------------------------

class SplitEndpointTests(StandardAPITest):

    def _commit(self, files, message, name="Carol Carter", email="carol@example.com"):
        for relpath, content in files:
            p = self.repo / relpath
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            subprocess.run(["git", "add", "--", relpath], cwd=str(self.repo),
                           check=True, capture_output=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
               "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
        subprocess.run(["git", "commit", "-m", message], cwd=str(self.repo),
                       check=True, capture_output=True, env=env)

    def _files_of(self, ref):
        r = subprocess.run(["git", "diff-tree", "--no-commit-id", "-r", "--name-only", ref],
                           cwd=str(self.repo), capture_output=True, text=True, check=True)
        return r.stdout.split()

    def test_split_middle_commit(self):
        # A two-file commit with a newer commit on top of it.
        self._commit([("alpha.txt", "A\n"), ("beta.txt", "B\n")], "Two files")
        self._commit([("gamma.txt", "G\n")], "Newer commit", name="Bob Brown", email="bob@example.com")
        state = self.get("/api/state").get_json()
        n_before = len(state["commits"])
        target = state["commits"][1]
        self.assertEqual(target["message"], "Two files")

        data = self.post("/api/rebase/split", json={
            "commit_hash": target["commit_hash"], "files_to_split": ["beta.txt"],
        }).get_json()
        self.assertTrue(data["ok"])
        commits = data["commits"]
        self.assertEqual(len(commits), n_before + 1)

        # The newer commit is replayed unchanged on top.
        self.assertEqual(commits[0]["message"], "Newer commit")
        self.assertEqual(self._files_of(commits[0]["commit_hash"]), ["gamma.txt"])

        # The target became two commits: split files newer, kept files older.
        split_commit, kept_commit = commits[1], commits[2]
        self.assertEqual([split_commit["message"], kept_commit["message"]], ["Two files", "Two files"])
        self.assertEqual([split_commit["author"], kept_commit["author"]], ["Carol Carter", "Carol Carter"])
        self.assertEqual(self._files_of(split_commit["commit_hash"]), ["beta.txt"])
        self.assertEqual(self._files_of(kept_commit["commit_hash"]), ["alpha.txt"])

        # Everything older than the target is untouched.
        self.assertEqual([c["message"] for c in commits[3:]],
                         [c["message"] for c in state["commits"][2:]])

    def test_split_tip_commit(self):
        self._commit([("alpha.txt", "A\n"), ("beta.txt", "B\n")], "Two files")
        state = self.get("/api/state").get_json()
        target = state["commits"][0]

        data = self.post("/api/rebase/split", json={
            "commit_hash": target["commit_hash"], "files_to_split": ["beta.txt"],
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["commits"]), len(state["commits"]) + 1)
        self.assertEqual(self._files_of(data["commits"][0]["commit_hash"]), ["beta.txt"])
        self.assertEqual(self._files_of(data["commits"][1]["commit_hash"]), ["alpha.txt"])

    def test_split_non_ascii_filename(self):
        # git diff-tree C-quotes non-ASCII paths by default; split must handle them
        self._commit([("ä.txt", "A\n"), ("beta.txt", "B\n")], "Non-ASCII file")
        state = self.get("/api/state").get_json()
        target = state["commits"][0]
        data = self.post("/api/rebase/split", json={
            "commit_hash": target["commit_hash"], "files_to_split": ["beta.txt"],
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["commits"]), len(state["commits"]) + 1)

    @pytest.mark.release
    def test_split_empty_files_refused(self):
        self._commit([("alpha.txt", "A\n"), ("beta.txt", "B\n")], "Two files")
        h = self.get("/api/state").get_json()["commits"][0]["commit_hash"]
        data = self.post("/api/rebase/split", json={ "commit_hash": h, "files_to_split": []}).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    @pytest.mark.release
    def test_split_all_files_refused(self):
        self._commit([("alpha.txt", "A\n"), ("beta.txt", "B\n")], "Two files")
        h = self.get("/api/state").get_json()["commits"][0]["commit_hash"]
        data = self.post("/api/rebase/split", json={
            "commit_hash": h, "files_to_split": ["alpha.txt", "beta.txt"],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    @pytest.mark.release
    def test_split_unknown_file_refused(self):
        self._commit([("alpha.txt", "A\n"), ("beta.txt", "B\n")], "Two files")
        h = self.get("/api/state").get_json()["commits"][0]["commit_hash"]
        data = self.post("/api/rebase/split", json={ "commit_hash": h, "files_to_split": ["nope.txt"]}).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    @pytest.mark.release
    def test_split_refused_when_dirty(self):
        self._commit([("alpha.txt", "A\n"), ("beta.txt", "B\n")], "Two files")
        h = self.get("/api/state").get_json()["commits"][0]["commit_hash"]
        self.make_dirty()
        data = self.post("/api/rebase/split", json={ "commit_hash": h, "files_to_split": ["beta.txt"]}).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")

    @pytest.mark.release
    def test_split_merge_commit_refused(self):
        merge = next(c for c in self.get("/api/state").get_json()["commits"]
                     if c["message"] == "Merge feature work")
        data = self.post("/api/rebase/split", json={
            "commit_hash": merge["commit_hash"], "files_to_split": ["feature-work.txt"],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")


# ---------------------------------------------------------------------------
# POST /api/rebase — invalid operation
# ---------------------------------------------------------------------------

class RebaseInvalidTests(StandardAPITest):

    @pytest.mark.release
    def test_unknown_operation_returns_404(self):
        resp = self.post("/api/rebase/unknown")
        self.assertEqual(resp.status_code, 404)

    @pytest.mark.release
    def test_missing_body_returns_error(self):
        data = self.post("/api/rebase/move").get_json()
        self.assertFalse(data["ok"])

    @pytest.mark.release
    def test_rebase_move_with_missing_order_field(self):
        data = self.post("/api/rebase/move", json={
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")


# ---------------------------------------------------------------------------
# POST /api/reset
# ---------------------------------------------------------------------------

class ResetEndpointTests(StandardAPITest):

    def test_reset_to_older_commit(self):
        state = self.get("/api/state").get_json()
        target = state["commits"][5]

        data = self.post("/api/reset", json={
            "commit_hash":target["commit_hash"],
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commits"][0]["commit_hash"], target["commit_hash"])

    @pytest.mark.release
    def test_reset_refused_when_dirty(self):
        self.make_dirty()
        h = self.get("/api/state").get_json()["commits"][5]["commit_hash"]

        data = self.post("/api/reset", json={"commit_hash":h}).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")

    @pytest.mark.release
    def test_reset_uses_post(self):
        resp = self.get("/api/reset")
        self.assertEqual(resp.status_code, 405)

    @pytest.mark.release
    def test_reset_with_missing_commit_hash(self):
        data = self.post("/api/reset", json={}).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_commit")


# ---------------------------------------------------------------------------
# POST /api/branch
# ---------------------------------------------------------------------------

class BranchEndpointTests(StandardAPITest):

    def test_create_branch_at_commit(self):
        state = self.get("/api/state").get_json()
        target_hash = state["commits"][2]["commit_hash"]

        data = self.post("/api/branch", json={"commit_hash": target_hash, "branch_name": "new-branch"}).get_json()
        self.assertTrue(data["ok"])
        self.assertIn("new-branch", data["branches"])
        target = next(c for c in data["commits"] if c["commit_hash"] == target_hash)
        self.assertIn("new-branch", target["branches"])

    @pytest.mark.release
    def test_create_branch_uses_post(self):
        resp = self.get("/api/branch")
        self.assertEqual(resp.status_code, 405)

    @pytest.mark.release
    def test_create_branch_with_invalid_commit_hash(self):
        data = self.post("/api/branch", json={"commit_hash": "deadbeef", "branch_name": "new-branch"}).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_commit")

    def test_delete_branch(self):
        h = self.get("/api/state").get_json()["commits"][2]["commit_hash"]
        self.post("/api/branch", json={"commit_hash": h, "branch_name": "feature/my-feature"})

        data = self.delete("/api/branch", json={"branch_name": "feature/my-feature"}).get_json()
        self.assertTrue(data["ok"])
        self.assertNotIn("feature/my-feature", data["branches"])

    @pytest.mark.release
    def test_delete_current_branch_refused(self):
        data = self.delete("/api/branch", json={"branch_name": self.initial_branch}).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "cannot_delete_current_branch")


    @pytest.mark.release
    def test_delete_nonexistent_branch(self):
        data = self.delete("/api/branch", json={"branch_name": "no-such-branch"}).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "git_failed")

    @pytest.mark.release
    def test_delete_unmerged_branch_refused(self):
        # A branch with a commit not reachable from HEAD cannot be safely deleted (git branch -d).
        repo = str(self.repo)
        subprocess.run(["git", "checkout", "-b", "feature/my-feature"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "unmerged"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "checkout", self.initial_branch], cwd=repo, check=True, capture_output=True)

        data = self.delete("/api/branch", json={"branch_name": "feature/my-feature"}).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "git_failed")


# ---------------------------------------------------------------------------
# GET /api/show
# ---------------------------------------------------------------------------

class ShowEndpointTests(StandardAPITest):

    def test_show_returns_info_and_diff(self):
        state = self.get("/api/state").get_json()
        head = state["commits"][0]

        data = self.get(f"/api/show?commit_hash={head['commit_hash']}").get_json()
        self.assertTrue(data["ok"])
        self.assertIn("diff --git", data["diff"])

    @pytest.mark.release
    def test_show_unknown_hash(self):
        data = self.get(f"/api/show?commit_hash={'0' * 40}").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_commit")

    @pytest.mark.release
    def test_show_missing_hash_param(self):
        data = self.get("/api/show").get_json()
        self.assertFalse(data["ok"])

    @pytest.mark.release
    def test_show_uses_get(self):
        resp = self.post("/api/show")
        self.assertEqual(resp.status_code, 405)

    def test_show_with_short_hash(self):
        head = self.get("/api/state").get_json()["commits"][0]
        data = self.get(f"/api/show?commit_hash={head['short_hash']}").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commit"]["commit_hash"], head["commit_hash"])

    def test_show_returns_files_list(self):
        # files lists the commit's changed paths (the canonical list split selects from).
        for name in ("one.txt", "two.txt"):
            (self.repo / name).write_text(name, encoding="utf-8")
            subprocess.run(["git", "add", name], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Two files"], cwd=str(self.repo), check=True, capture_output=True)
        head = self.get("/api/state").get_json()["commits"][0]
        data = self.get(f"/api/show?commit_hash={head['commit_hash']}").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(sorted(data["files"]), ["one.txt", "two.txt"])

    @pytest.mark.release
    def test_show_rename_displays_rename_but_files_are_canonical(self):
        # The diff shows a rename, while files stays delete+add (matching split's
        # diff-tree view) so both name the same paths split validates against.
        subprocess.run(["git", "config", "diff.renames", "true"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "mv", "README.md", "READYOU.md"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Rename readme"], cwd=str(self.repo), check=True, capture_output=True)
        head = self.get("/api/state").get_json()["commits"][0]
        data = self.get(f"/api/show?commit_hash={head['commit_hash']}").get_json()
        self.assertTrue(data["ok"])
        self.assertIn("rename from README.md", data["diff"])
        self.assertIn("rename to READYOU.md", data["diff"])
        self.assertEqual(sorted(data["files"]), ["README.md", "READYOU.md"])


# ---------------------------------------------------------------------------
# (Staged) (index) row
# ---------------------------------------------------------------------------

class IndexRowTests(StandardAPITest):
    """Tests for the synthetic 'index' row shown when staged changes exist."""

    @pytest.mark.release
    def test_state_no_index_row_when_clean(self):
        state = self.get("/api/state").get_json()
        hashes = [c["commit_hash"] for c in state["commits"]]
        self.assertNotIn("(Staged)", hashes)

    def test_state_shows_index_row_when_staged(self):
        self.make_staged()
        state = self.get("/api/state").get_json()
        self.assertEqual(state["commits"][0]["commit_hash"], "(Staged)")

    @pytest.mark.release
    def test_index_row_label(self):
        self.make_staged()
        state = self.get("/api/state").get_json()
        index_commit = state["commits"][0]
        self.assertEqual(index_commit["short_hash"], "(Staged)")
        self.assertIn("Staged", index_commit["message"])

    @pytest.mark.release
    def test_index_row_disappears_after_unstage(self):
        self.make_staged()
        state = self.get("/api/state").get_json()
        self.assertEqual(state["commits"][0]["commit_hash"], "(Staged)")
        subprocess.run(["git", "restore", "--staged", "README.md"],
                       cwd=str(self.repo), check=True, capture_output=True)
        state2 = self.get("/api/state").get_json()
        hashes = [c["commit_hash"] for c in state2["commits"]]
        self.assertNotIn("(Staged)", hashes)

    def test_show_index_returns_diff(self):
        self.make_staged()
        data = self.get("/api/show?commit_hash=%28Staged%29").get_json()
        self.assertTrue(data["ok"])
        self.assertIn("diff --git", data["diff"])

    @pytest.mark.release
    def test_show_index_commit_fields(self):
        self.make_staged()
        data = self.get("/api/show?commit_hash=%28Staged%29").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commit"]["commit_hash"], "(Staged)")
        self.assertEqual(data["commit"]["short_hash"], "(Staged)")

    @pytest.mark.release
    def test_show_index_without_staged_changes_returns_empty_diff(self):
        data = self.get("/api/show?commit_hash=%28Staged%29").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["diff"], "")

    @pytest.mark.release
    def test_show_index_has_no_files_list(self):
        # The (Staged) row carries no files list, so Split stays unavailable there.
        self.make_staged()
        data = self.get("/api/show?commit_hash=%28Staged%29").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["files"], [])

    def test_rebase_move_ignores_index_hash(self):
        self.make_staged()
        state = self.get("/api/state").get_json()
        # Build order that excludes index; index should not be movable
        order = [c["commit_hash"] for c in state["commits"] if c["commit_hash"] != "(Staged)"]
        result = self.post("/api/rebase/move", json={ "order": order}).get_json()
        self.assertTrue(result["ok"])

    def test_rebase_squash_rejects_index(self):
        self.make_staged()
        state = self.get("/api/state").get_json()
        real_commits = [c["commit_hash"] for c in state["commits"] if c["commit_hash"] != "(Staged)"]
        result = self.post("/api/rebase/squash", json={
            "commit_hashes": ["(Staged)", real_commits[0]],
        }).get_json()
        self.assertFalse(result["ok"])

    def test_rebase_fixup_rejects_index(self):
        self.make_staged()
        state = self.get("/api/state").get_json()
        real_commits = [c["commit_hash"] for c in state["commits"] if c["commit_hash"] != "(Staged)"]
        result = self.post("/api/rebase/fixup", json={
            "commit_hashes": ["(Staged)", real_commits[0]],
        }).get_json()
        self.assertFalse(result["ok"])

    def test_rebase_reword_rejects_index(self):
        result = self.post("/api/rebase/reword", json={
            "commit_hashes": ["(Staged)"],
            "new_message": "new message",
        }).get_json()
        self.assertFalse(result["ok"])


# ---------------------------------------------------------------------------
# Conflict flow via REST
# ---------------------------------------------------------------------------

class ConflictEndpointTests(StandardAPITest):
    """Test the full conflict workflow through the API."""

    def _swap_order(self):
        """Swap the two conflict commits to trigger a conflict."""
        state = self.get("/api/state").get_json()
        order = [c["commit_hash"] for c in state["commits"]]
        # Find the two conflict commits
        i_a = next(i for i, c in enumerate(state["commits"])
                   if c["message"] == "conflict: version A")
        i_b = next(i for i, c in enumerate(state["commits"])
                   if c["message"] == "conflict: version B")
        # Swap them (version B should come after version A)
        order[i_a], order[i_b] = order[i_b], order[i_a]
        return self.post("/api/rebase/move", json={ "order": order})

    @pytest.mark.release
    def test_move_produces_conflict(self):
        data = self._swap_order().get_json()
        self.assertFalse(data["ok"])
        self.assertTrue(data.get("conflict"))
        self.assertIn("README.md", data["conflict_files"])
        self.assertTrue(data["rebase_in_progress"])
        # Conflict response must carry the full state fields, not just conflict
        # info, so the frontend can refresh uniformly.
        for field in ("branch", "branches", "commits", "undo_stack",
                      "dirty", "has_stash", "submodule_update_suggested"):
            self.assertIn(field, data)
        self.assertTrue(data["commits"])

    def test_abort_clears_conflict(self):
        self._swap_order()
        data = self.post("/api/rebase/abort").get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["rebase_in_progress"])
        self.assertEqual(data["conflict_files"], [])

    def test_continue_with_unresolved_conflict(self):
        self._swap_order()
        data = self.post("/api/rebase/continue").get_json()
        self.assertFalse(data["ok"])
        self.assertTrue(data.get("conflict"))
        self.assertTrue(data["rebase_in_progress"])

    def test_continue_after_resolving(self):
        self._swap_order()
        # Resolve to initial content so version B's patch applies cleanly next.
        (self.repo / "README.md").write_bytes(b"# Todo App\n\nA simple web application for managing tasks.\n")
        subprocess.run(["git", "add", "README.md"], cwd=str(self.repo),
                       check=True, capture_output=True)

        data = self.post("/api/rebase/continue").get_json()
        self.assertTrue(data["ok"], f"Rebase failed: {data.get('error', 'unknown')}")
        self.assertFalse(data["rebase_in_progress"])

    @pytest.mark.release
    def test_rebase_continue_uses_post(self):
        resp = self.client.get("/api/rebase/continue",
                               headers={"X-Token": TOKEN})
        self.assertEqual(resp.status_code, 405)

    @pytest.mark.release
    def test_rebase_abort_uses_post(self):
        resp = self.client.get("/api/rebase/abort",
                               headers={"X-Token": TOKEN})
        self.assertEqual(resp.status_code, 405)

    @pytest.mark.release
    def test_rebase_continue_when_not_in_rebase_returns_error(self):
        data = self.post("/api/rebase/continue").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "not_in_rebase")

    @pytest.mark.release
    def test_rebase_abort_when_not_in_rebase_returns_error(self):
        data = self.post("/api/rebase/abort").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "not_in_rebase")


# ---------------------------------------------------------------------------
# POST /api/quit
# ---------------------------------------------------------------------------

class QuitEndpointTests(StandardAPITest):

    @pytest.mark.release
    def test_quit_returns_ok(self):
        with patch("git_warp.os._exit"):
            data = self.post("/api/quit").get_json()
        self.assertTrue(data["ok"])

    @pytest.mark.release
    def test_quit_uses_post(self):
        resp = self.get("/api/quit")
        self.assertEqual(resp.status_code, 405)


@pytest.mark.release
class ManualPageTests(StandardAPITest):

    def test_manual_returns_200(self):
        resp = self.client.get("/manual")
        self.assertEqual(resp.status_code, 200)

    def test_manual_contains_html(self):
        resp = self.client.get("/manual")
        self.assertIn(b"git-warp", resp.data)

    def test_manual_requires_no_token(self):
        resp = self.client.get("/manual")
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# POST /api/rebase — consecutive commit validation
# ---------------------------------------------------------------------------

class RebaseConsecutiveEndpointTests(StandardAPITest):

    @pytest.mark.release
    def test_fixup_non_adjacent_commits_returns_invalid_request(self):
        state = self.get("/api/state").get_json()
        h0 = state["commits"][0]["commit_hash"]
        h2 = state["commits"][2]["commit_hash"]

        data = self.post("/api/rebase/fixup", json={
            "commit_hashes": [h0, h2],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    def test_squash_three_adjacent_commits_is_valid(self):
        state = self.get("/api/state").get_json()
        hashes = [state["commits"][i]["commit_hash"] for i in range(3)]
        data = self.post("/api/rebase/squash", json={ "commit_hashes": hashes}).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["commits"]), len(state["commits"]) - 2)

    @pytest.mark.release
    def test_squash_non_adjacent_rejects_and_preserves_history(self):
        state = self.get("/api/state").get_json()
        hashes_before = [c["commit_hash"] for c in state["commits"]]

        data = self.post("/api/rebase/squash", json={
            "commit_hashes": [hashes_before[0], hashes_before[2]],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

        hashes_after = [c["commit_hash"] for c in self.get("/api/state").get_json()["commits"]]
        self.assertEqual(hashes_after, hashes_before)


# ---------------------------------------------------------------------------
# POST /api/submodule/update
# ---------------------------------------------------------------------------

@pytest.mark.submodule
class SubmoduleUpdateEndpointTests(StandardAPITest):

    def test_submodule_update_uses_post(self):
        resp = self.get("/api/submodule/update")
        self.assertEqual(resp.status_code, 405)

    def test_submodule_update_requires_auth(self):
        resp = self.client.post("/api/submodule/update")
        self.assertEqual(resp.status_code, 403)

    def test_submodule_update_returns_state(self):
        data = self.post("/api/submodule/update").get_json()
        self.assertTrue(data["ok"])
        self.assertIn("commits", data)
        self.assertIn("branch", data)
        self.assertIn("dirty", data)

    @pytest.mark.release
    def test_submodule_update_blocked_during_rebase(self):
        # Trigger a conflict to put repo in rebase state
        state = self.get("/api/state").get_json()
        order = [c["commit_hash"] for c in state["commits"]]
        i_a = next(i for i, c in enumerate(state["commits"]) if c["message"] == "conflict: version A")
        i_b = next(i for i, c in enumerate(state["commits"]) if c["message"] == "conflict: version B")
        order[i_a], order[i_b] = order[i_b], order[i_a]
        self.post("/api/rebase/move", json={ "order": order})
        # Now try submodule_update
        result = self.post("/api/submodule/update").get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rebase_in_progress")


# ---------------------------------------------------------------------------
# GET /log
# ---------------------------------------------------------------------------

@pytest.mark.release
class LogEndpointTests(StandardAPITest):

    def setUp(self):
        super().setUp()
        # Populate branch reflog: detach and reattach HEAD to populate reflog with all commits
        subprocess.run(["git", "checkout", "-q", "HEAD~0"], cwd=str(self.repo),
                       capture_output=True, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=str(self.repo),
                       capture_output=True, check=True)
        self._log_file = self.tmpdir / "test.log"
        # Delete log file to start fresh for each test
        self._log_file.unlink(missing_ok=True)
        self.app = create_app(str(self.repo), TOKEN, log_path=self._log_file)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        super().tearDown()

    def test_log_returns_200_when_file_does_not_exist(self):
        resp = self.get("/log")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"")

    def test_log_returns_plain_text_content_type(self):
        resp = self.get("/log")
        self.assertIn("text/plain", resp.content_type)

    def test_log_does_not_require_auth(self):
        resp = self.client.get("/log")
        self.assertEqual(resp.status_code, 200)

    def test_log_returns_entries_after_reset(self):
        state = self.get("/api/state").get_json()
        self.post("/api/reset", json={"commit_hash":state["commits"][3]["commit_hash"]})

        resp = self.get("/log")
        lines = [l for l in resp.data.decode("utf-8").splitlines() if l]
        self.assertEqual(len(lines), 1)
        parts = lines[0].split()
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[1], "main")
        self.assertEqual(len(parts[2]), 40)

    def test_log_accumulates_entries_across_operations(self):
        state = self.get("/api/state").get_json()
        self.post("/api/reset", json={"commit_hash":state["commits"][2]["commit_hash"]})

        state2 = self.get("/api/state").get_json()
        self.post("/api/rebase/reword", json={
            "commit_hashes": [state2["commits"][0]["commit_hash"]],
            "new_message": "log test reword",
        })

        lines = [l for l in self.get("/log").data.decode("utf-8").splitlines() if l]
        self.assertEqual(len(lines), 2)

    def test_log_does_not_append_entry_for_failed_operation(self):
        self.post("/api/reset", json={"commit_hash": "0" * 40})
        self.assertEqual(self.get("/log").data, b"")


# ---------------------------------------------------------------------------
# Auth token edge cases
# ---------------------------------------------------------------------------

class AuthTokenEdgeCasesTests(StandardAPITest):

    @pytest.mark.release
    def test_empty_token_returns_403(self):
        resp = self.client.get("/api/state", headers={"X-Token": ""})
        self.assertEqual(resp.status_code, 403)

    @pytest.mark.release
    def test_token_with_spaces_returns_403(self):
        resp = self.client.get("/api/state",
                               headers={"X-Token": "test-token abc123"})
        self.assertEqual(resp.status_code, 403)

    @pytest.mark.release
    def test_token_case_sensitive(self):
        resp = self.client.get("/api/state",
                               headers={"X-Token": TOKEN.upper()})
        self.assertEqual(resp.status_code, 403)

    @pytest.mark.release
    def test_token_with_special_characters_wrong(self):
        resp = self.client.get("/api/state",
                               headers={"X-Token": TOKEN + "!"})
        self.assertEqual(resp.status_code, 403)

    @pytest.mark.release
    def test_token_in_query_string_vs_header(self):
        # Query string token is rejected; the header authenticates.
        resp = self.client.get(f"/api/state?t={TOKEN}")
        self.assertEqual(resp.status_code, 403)
        resp = self.client.get("/api/state", headers={"X-Token": TOKEN})
        self.assertEqual(resp.status_code, 200)

    @pytest.mark.release
    def test_wrong_query_token_returns_403(self):
        resp = self.client.get("/api/state?t=wrong")
        self.assertEqual(resp.status_code, 403)

    @pytest.mark.release
    def test_malformed_json_with_valid_token(self):
        resp = self.client.post("/api/rebase/move",
                                data="{invalid json}",
                                headers={"X-Token": TOKEN,
                                        "Content-Type": "application/json"})
        # Should fail on JSON parsing, not auth
        self.assertNotEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# Stash conflict scenarios
# ---------------------------------------------------------------------------

@pytest.mark.release
class StashConflictScenarioTests(StandardAPITest):

    def test_stash_pop_with_uncommitted_changes_after_stash(self):
        # Make dirty changes and stash them
        self.make_dirty()
        self.post("/api/stash")

        # Make different dirty changes
        self.make_dirty(path="LICENSE", content=b"different content\n")

        # Pop should fail due to dirty tree
        data = self.post("/api/stash/pop").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")

    def test_stash_then_reset_then_pop(self):
        state = self.get("/api/state").get_json()
        self.make_dirty()
        self.post("/api/stash")

        # Reset to an older commit (changes state). commits[1] is "conflict: version B",
        # whose README.md matches HEAD's, so the stashed README change applies cleanly.
        self.post("/api/reset", json={"commit_hash": state["commits"][1]["commit_hash"]})

        # Pop should still work on the stashed content
        data = self.post("/api/stash/pop").get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["dirty"])

    @pytest.mark.release
    def test_stash_pop_conflict_returns_stash_conflict(self):
        self.make_dirty()
        self.post("/api/stash")
        # Commit a conflicting change to the same file on HEAD
        (self.repo / "README.md").write_bytes(b"# conflicting HEAD change\n")
        subprocess.run(["git", "add", "README.md"], cwd=str(self.repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "conflicting change"], cwd=str(self.repo), capture_output=True)
        data = self.post("/api/stash/pop").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "stash_conflict")


# ---------------------------------------------------------------------------
# HTTP method validation
# ---------------------------------------------------------------------------

@pytest.mark.release
class HTTPMethodValidationTests(StandardAPITest):

    def test_state_rejects_post(self):
        resp = self.post("/api/state")
        self.assertEqual(resp.status_code, 405)

    def test_stash_rejects_get(self):
        resp = self.get("/api/stash")
        self.assertEqual(resp.status_code, 405)

    def test_show_rejects_post(self):
        resp = self.post("/api/show")
        self.assertEqual(resp.status_code, 405)

    def test_rebase_rejects_get(self):
        resp = self.get("/api/rebase/move")
        self.assertEqual(resp.status_code, 405)


# ---------------------------------------------------------------------------
# Error response structure
# ---------------------------------------------------------------------------

@pytest.mark.release
class ErrorResponseStructureTests(StandardAPITest):

    def test_error_response_has_ok_false(self):
        data = self.post("/api/stash").get_json()
        self.assertFalse(data["ok"])

    def test_error_response_has_error_field(self):
        data = self.post("/api/stash").get_json()
        self.assertIn("error", data)

    def test_invalid_operation_error_response(self):
        resp = self.post("/api/rebase/nonexistent")
        self.assertEqual(resp.status_code, 404)

    def test_missing_required_field_error_response(self):
        data = self.post("/api/rebase/move", json={
            # Missing "order" field
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")


# ---------------------------------------------------------------------------
# Undo stack labels
# ---------------------------------------------------------------------------

class UndoStackLabelTests(StandardAPITest):

    @pytest.mark.release
    def test_sequential_operations_produce_expected_labels(self):
        state = self.get("/api/state").get_json()

        # reword the newest commit
        h0 = state["commits"][0]["commit_hash"]
        r1 = self.post("/api/rebase/reword", json={
            "commit_hashes": [h0], "new_message": "Rewarded top",
        }).get_json()
        self.assertTrue(r1["ok"])
        entries = [e for e in r1["undo_stack"] if e["label"].startswith("reword:")]
        self.assertEqual(len(entries), 1)
        self.assertIn("Rewarded top", entries[0]["label"])

        # move: swap the two newest commits
        order = [c["commit_hash"] for c in r1["commits"]]
        order[0], order[1] = order[1], order[0]
        r2 = self.post("/api/rebase/move", json={"order": order}).get_json()
        self.assertTrue(r2["ok"])
        entries = [e for e in r2["undo_stack"] if e["label"].startswith("reorder")]
        self.assertEqual(len(entries), 1)

        # squash: fold the two newest (now reordered) commits into the older one
        dest_msg = r2["commits"][1]["message"]
        hashes = [r2["commits"][0]["commit_hash"], r2["commits"][1]["commit_hash"]]
        r3 = self.post("/api/rebase/squash", json={"commit_hashes": hashes}).get_json()
        self.assertTrue(r3["ok"])
        entries = [e for e in r3["undo_stack"] if e["label"].startswith("squash:")]
        self.assertEqual(len(entries), 1)
        self.assertIn(dest_msg.split("\n")[0], entries[0]["label"])

        # fixup: fold the new top commit into its parent
        dest_msg = r3["commits"][1]["message"]
        r4 = self.post("/api/rebase/fixup", json={
            "commit_hashes": [r3["commits"][0]["commit_hash"]],
        }).get_json()
        self.assertTrue(r4["ok"])
        entries = [e for e in r4["undo_stack"] if e["label"].startswith("fixup:")]
        self.assertEqual(len(entries), 1)
        self.assertIn(dest_msg.split("\n")[0], entries[0]["label"])

        # reset to an earlier commit
        target = r4["commits"][3]
        r5 = self.post("/api/reset", json={"commit_hash": target["commit_hash"]}).get_json()
        self.assertTrue(r5["ok"])
        entries = [e for e in r5["undo_stack"] if e["label"].startswith("reset:")]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["label"], f"reset: {target['message'].split(chr(10))[0]}")
        self.assertNotIn("\n", entries[0]["label"])

    @pytest.mark.release
    def test_squash_three_commits_names_destination_once(self):
        # All three fold into the oldest selected commit; the label names that
        # single destination once, not once per folded commit.
        state = self.get("/api/state").get_json()
        dest_subject = state["commits"][2]["message"].split("\n")[0]
        hashes = [state["commits"][i]["commit_hash"] for i in range(3)]
        data = self.post("/api/rebase/squash", json={ "commit_hashes": hashes}).get_json()
        self.assertTrue(data["ok"])
        entries = [e for e in data["undo_stack"] if e["label"].startswith("squash:")]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["label"], "squash: " + dest_subject)


    @pytest.mark.release
    def test_mixed_operation_rebase_produces_generic_rebase_label(self):
        """Mixed rebase (reword + fixup) done outside git-warp should label as 'rebase'."""
        state = self.get("/api/state").get_json()
        self.assertGreaterEqual(len(state["commits"]), 3, "Need at least 3 commits for this test")
        h0 = state["commits"][0]["commit_hash"]  # newest
        h1 = state["commits"][1]["commit_hash"]  # second newest
        h2 = state["commits"][2]["commit_hash"]  # rebase base
        # Create temp files for rebase todo and commit message
        todo_path = self.tmpdir / "rebase_todo"
        msg_path = self.tmpdir / "rebase_msg"
        todo_path.write_text(f"reword {h1}\nfixup {h0}\n")
        msg_path.write_text("Mixed operation reworded commit\n")
        # Set up environment to use the editor shim (same pattern as backend._rebase_env)
        editor_py = REPO_ROOT / "git_warp" / "editor.py"
        editor_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(editor_py))}"
        env = os.environ.copy()
        env["GIT_SEQUENCE_EDITOR"] = editor_cmd
        env["GIT_EDITOR"] = editor_cmd
        env["GIT_WARP_TODO"] = str(todo_path)
        env["GIT_WARP_MSG"] = str(msg_path)
        # Run the mixed rebase outside git-warp
        result = subprocess.run(
            ["git", "rebase", "-i", h2],
            cwd=str(self.repo),
            env=env,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, f"rebase failed: {result.stderr.decode()}")
        # Fetch state and verify the mixed rebase is labeled "rebase"
        state = self.get("/api/state").get_json()
        entries = [e for e in state["undo_stack"] if e["label"] == "rebase"]
        self.assertGreater(len(entries), 0, "Expected a 'rebase' label for mixed operations (reword + fixup)")


# ---------------------------------------------------------------------------
# Sequential operations
# ---------------------------------------------------------------------------

class SequentialOpsTests(StandardAPITest):

    @pytest.mark.release
    def test_multiple_squash_operations_in_sequence(self):
        state = self.get("/api/state").get_json()
        hashes = [c["commit_hash"] for c in state["commits"]]
        r1 = self.post("/api/rebase/squash", json={ "commit_hashes": [hashes[0], hashes[1]]}).get_json()
        self.assertTrue(r1["ok"])
        r2 = self.post("/api/rebase/squash", json={ "commit_hashes": [r1["commits"][0]["commit_hash"], r1["commits"][1]["commit_hash"]]}).get_json()
        self.assertTrue(r2["ok"])
        self.assertEqual(len(r2["commits"]), len(r1["commits"]) - 1)

    @pytest.mark.release
    def test_reword_then_move(self):
        state = self.get("/api/state").get_json()
        h0 = state["commits"][0]["commit_hash"]
        r1 = self.post("/api/rebase/reword", json={ "commit_hashes": [h0], "new_message": "Reworded"}).get_json()
        self.assertTrue(r1["ok"])
        order = [c["commit_hash"] for c in r1["commits"]]
        order[0], order[1] = order[1], order[0]
        r2 = self.post("/api/rebase/move", json={ "order": order}).get_json()
        self.assertTrue(r2["ok"])

    @pytest.mark.release
    def test_move_then_squash_then_reset(self):
        state = self.get("/api/state").get_json()
        orig_hashes = [c["commit_hash"] for c in state["commits"]]
        order = orig_hashes[:]
        order[0], order[1] = order[1], order[0]
        r1 = self.post("/api/rebase/move", json={ "order": order}).get_json()
        self.assertTrue(r1["ok"])
        r2 = self.post("/api/rebase/squash", json={ "commit_hashes": [r1["commits"][0]["commit_hash"], r1["commits"][1]["commit_hash"]]}).get_json()
        self.assertTrue(r2["ok"])
        r3 = self.post("/api/reset", json={"commit_hash": orig_hashes[0]}).get_json()
        self.assertTrue(r3["ok"])


# ---------------------------------------------------------------------------
# Edge cases: filenames with Unicode and whitespace
# ---------------------------------------------------------------------------

@pytest.mark.release
class FilenameEdgeCasesTests(StandardAPITest):

    def test_move_with_complex_filename(self):
        filename = "report (café) [v2].txt"
        (self.repo / filename).write_bytes("report content\n".encode("utf-8"))
        subprocess.run(["git", "add", filename], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add complex filename"], cwd=str(self.repo), check=True, capture_output=True)

        state = self.get("/api/state").get_json()
        order = [c["commit_hash"] for c in state["commits"]]
        order[0], order[1] = order[1], order[0]
        data = self.post("/api/rebase/move", json={ "order": order}).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commits"][1]["message"], "Add complex filename")

    def test_squash_and_show_with_complex_filename(self):
        filename = "file with spaces (日本語) [draft].txt"
        (self.repo / filename).write_bytes("content\n".encode("utf-8"))
        subprocess.run(["git", "add", filename], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add Japanese file"], cwd=str(self.repo), check=True, capture_output=True)

        state = self.get("/api/state").get_json()
        hashes = [state["commits"][0]["commit_hash"], state["commits"][1]["commit_hash"]]
        r1 = self.post("/api/rebase/squash", json={ "commit_hashes": hashes}).get_json()
        self.assertTrue(r1["ok"])
        self.assertEqual(len(r1["commits"]), len(state["commits"]) - 1)
        r2 = self.get(f"/api/show?commit_hash={r1['commits'][0]['commit_hash']}").get_json()
        self.assertTrue(r2["ok"])
        self.assertIn("diff --git", r2["diff"])


# ---------------------------------------------------------------------------
# Edge cases: reset with deleted files
# ---------------------------------------------------------------------------

@pytest.mark.release
class ResetDeletedFilesTests(StandardAPITest):

    def test_reset_restores_deleted_files(self):
        state = self.get("/api/state").get_json()
        head_hash = state["commits"][0]["commit_hash"]
        (self.repo / "README.md").unlink()
        (self.repo / "LICENSE").unlink()
        subprocess.run(["git", "add", "README.md", "LICENSE"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Delete files"], cwd=str(self.repo), check=True, capture_output=True)

        data = self.post("/api/reset", json={"commit_hash": head_hash}).get_json()
        self.assertTrue(data["ok"])
        self.assertTrue((self.repo / "README.md").exists())
        self.assertTrue((self.repo / "LICENSE").exists())

    def test_show_deleted_file_in_diff(self):
        (self.repo / "LICENSE").unlink()
        subprocess.run(["git", "add", "LICENSE"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Remove LICENSE"], cwd=str(self.repo), check=True, capture_output=True)

        state = self.get("/api/state").get_json()
        data = self.get(f"/api/show?commit_hash={state['commits'][0]['commit_hash']}").get_json()
        self.assertTrue(data["ok"])
        self.assertIn("LICENSE", data["diff"])
        self.assertIn("deleted file mode", data["diff"])


# ---------------------------------------------------------------------------
# Switch branch
# ---------------------------------------------------------------------------

class SwitchBranchEndpointTests(StandardAPITest):

    def setUp(self):
        super().setUp()
        # Ensure feature branch points to initial_head (in case previous test moved it)
        subprocess.run(
            ["git", "branch", "-f", "feature", self.__class__.initial_head],
            cwd=str(self.repo), check=True, capture_output=True,
        )

    @pytest.mark.release
    def test_state_includes_branches_list(self):
        state = self.get("/api/state").get_json()
        self.assertIn("main", state["branches"])

    @pytest.mark.release
    def test_branches_list_includes_all_local_branches(self):
        state = self.get("/api/state").get_json()
        self.assertIn("feature", state["branches"])

    def test_switch_to_existing_branch(self):
        result = self.post("/api/switch", json={"branch": "feature"}).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["branch"], "feature")

    @pytest.mark.release
    def test_switch_returns_full_state(self):
        result = self.post("/api/switch", json={"branch": "feature"}).get_json()
        self.assertIsNotNone(result["commits"])
        self.assertIsNotNone(result["undo_stack"])
        self.assertIsNotNone(result["branches"])

    @pytest.mark.release
    def test_switch_to_unknown_branch_returns_error(self):
        result = self.post("/api/switch", json={"branch": "nonexistent"}).get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_branch")

    @pytest.mark.release
    def test_switch_empty_branch_returns_error(self):
        result = self.post("/api/switch", json={"branch": ""}).get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_branch")

    @pytest.mark.release
    def test_switch_when_dirty_is_refused(self):
        self.make_dirty()
        result = self.post("/api/switch", json={"branch": "feature"}).get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "dirty_tree")

    def _start_conflict_rebase(self):
        """Swap the two conflict commits to trigger a conflict."""
        state = self.get("/api/state").get_json()
        order = [c["commit_hash"] for c in state["commits"]]
        i_a = next(i for i, c in enumerate(state["commits"]) if c["message"] == "conflict: version A")
        i_b = next(i for i, c in enumerate(state["commits"]) if c["message"] == "conflict: version B")
        order[i_a], order[i_b] = order[i_b], order[i_a]
        self.post("/api/rebase/move", json={ "order": order})

    @pytest.mark.release
    def test_switch_during_rebase_is_refused(self):
        self._start_conflict_rebase()
        result = self.post("/api/switch", json={"branch": "feature"}).get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rebase_in_progress")

    @pytest.mark.release
    def test_stash_during_rebase_is_refused(self):
        self._start_conflict_rebase()
        result = self.post("/api/stash").get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rebase_in_progress")

    @pytest.mark.release
    def test_stash_pop_during_rebase_is_refused(self):
        self._start_conflict_rebase()
        result = self.post("/api/stash/pop").get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rebase_in_progress")

    @pytest.mark.release
    def test_reset_during_rebase_is_refused(self):
        state = self.get("/api/state").get_json()
        self._start_conflict_rebase()
        result = self.post("/api/reset", json={"commit_hash": state["commits"][0]["commit_hash"]}).get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rebase_in_progress")

    @pytest.mark.release
    def test_switch_and_list_branches_with_special_characters(self):
        subprocess.run(["git", "branch", "-d", "feature"],
                       cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "branch", "feature/my-feature"],
                       cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "branch", "bugfix/issue-123"],
                       cwd=str(self.repo), check=True, capture_output=True)

        result = self.post("/api/switch", json={"branch": "feature/my-feature"}).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["branch"], "feature/my-feature")

        state = self.get("/api/state").get_json()
        self.assertIn("feature/my-feature", state["branches"])
        self.assertIn("bugfix/issue-123", state["branches"])


# ---------------------------------------------------------------------------
# Detached HEAD: mutations refused; only selecting a branch is allowed
# ---------------------------------------------------------------------------

class DetachedHeadEndpointTests(StandardAPITest):

    def detach(self):
        # Detach at the current commit, leaving the same commits visible.
        subprocess.run(["git", "checkout", "--detach", "HEAD"],
                       cwd=str(self.repo), check=True, capture_output=True)

    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.repo),
                              capture_output=True, text=True, check=True).stdout.strip()

    @pytest.mark.release
    def test_state_reports_empty_branch_when_detached(self):
        self.detach()
        state = self.get("/api/state").get_json()
        self.assertTrue(state["ok"])
        self.assertEqual(state["branch"], "")
        self.assertFalse(state["rebase_in_progress"])
        self.assertTrue(state["commits"])  # commits still listed from detached HEAD

    @pytest.mark.release
    def test_reset_refused_when_detached(self):
        state = self.get("/api/state").get_json()
        target = state["commits"][2]["commit_hash"]
        self.detach()
        before = self.head()
        result = self.post("/api/reset", json={"commit_hash": target}).get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "detached_head")
        self.assertEqual(self.head(), before)  # repo untouched

    @pytest.mark.release
    def test_rebase_move_refused_when_detached(self):
        state = self.get("/api/state").get_json()
        order = [c["commit_hash"] for c in state["commits"]]
        order[0], order[1] = order[1], order[0]
        self.detach()
        before = self.head()
        result = self.post("/api/rebase/move", json={ "order": order}).get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "detached_head")
        self.assertEqual(self.head(), before)

    @pytest.mark.release
    def test_stash_refused_when_detached(self):
        self.detach()
        result = self.post("/api/stash").get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "detached_head")

    @pytest.mark.release
    def test_submodule_update_refused_when_detached(self):
        self.detach()
        result = self.post("/api/submodule/update").get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "detached_head")

    def test_switch_from_detached_attaches_branch(self):
        self.detach()
        result = self.post("/api/switch", json={"branch": "main"}).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["branch"], "main")


# ---------------------------------------------------------------------------
# Pushed status
# ---------------------------------------------------------------------------

@pytest.mark.release
class PushedEndpointTests(StandardAPITest):

    def _add_remote_and_push(self):
        bare = self.tmpdir / "bare"
        shutil.rmtree(bare, ignore_errors=True)
        bare.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare)],
                       cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "main"],
                       cwd=str(self.repo), check=True, capture_output=True)

    def test_no_remote_all_unpushed(self):
        state = self.get("/api/state").get_json()
        self.assertTrue(all(not c["pushed"] for c in state["commits"]))

    def test_all_commits_pushed_after_push(self):
        self._add_remote_and_push()
        state = self.get("/api/state").get_json()
        self.assertTrue(all(c["pushed"] for c in state["commits"]))

    def test_new_commit_after_push_is_unpushed(self):
        self._add_remote_and_push()
        _commit_raw(self.repo, "new.txt", b"new\n", "New unpushed commit", "alice", 100)
        state = self.get("/api/state").get_json()
        self.assertFalse(state["commits"][0]["pushed"])
        self.assertTrue(all(c["pushed"] for c in state["commits"][1:]))


# ---------------------------------------------------------------------------
# Edge cases: empty repository
# ---------------------------------------------------------------------------

@pytest.mark.release
class EmptyRepoEndpointTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-empty-api-"))
        self.repo = self.tmpdir / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(self.repo),
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"],
                       cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=str(self.repo), check=True, capture_output=True)
        self.app = create_app(str(self.repo), TOKEN)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def get(self, url, **kwargs):
        return self.client.get(url, headers={"X-Token": TOKEN}, **kwargs)

    def post(self, url, json=None, **kwargs):
        return self.client.post(url, json=json, headers={"X-Token": TOKEN}, **kwargs)

    def test_empty_repo_read_state_returns_empty_commits(self):
        state = self.get("/api/state").get_json()
        self.assertEqual(len(state["commits"]), 0)
        self.assertEqual(state["undo_stack"], [])
        self.assertFalse(state["dirty"])

    def test_empty_repo_reports_real_branch_name(self):
        # The "## No commits yet on <branch>" header must not be misparsed as "No".
        expected = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"],
                                   cwd=str(self.repo), capture_output=True, text=True).stdout.strip()
        state = self.get("/api/state").get_json()
        self.assertEqual(state["branch"], expected)
        self.assertNotEqual(state["branch"], "No")

    def test_empty_repo_operations_fail_gracefully(self):
        result = self.post("/api/reset", json={"commit_hash": "0" * 40}).get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_commit")


# ---------------------------------------------------------------------------
# Edge cases: show with binary files
# ---------------------------------------------------------------------------

@pytest.mark.release
class ShowBinaryEndpointTests(StandardAPITest):

    def test_show_commit_with_binary_file(self):
        binary_path = self.repo / "binary.bin"
        binary_path.write_bytes(b"\x00\x01\x02\x03\x04\x05")
        subprocess.run(["git", "add", "binary.bin"], cwd=str(self.repo),
                       check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add binary file"],
                       cwd=str(self.repo), check=True, capture_output=True)

        state = self.get("/api/state").get_json()
        result = self.get(f"/api/show?commit_hash={state['commits'][0]['commit_hash']}").get_json()

        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["diff"])
        self.assertIn("binary", result["diff"].lower())

    def test_show_modifies_binary_file(self):
        binary_path = self.repo / "binary.bin"
        binary_path.write_bytes(b"\x00\x01\x02")
        subprocess.run(["git", "add", "binary.bin"], cwd=str(self.repo),
                       check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Create binary"],
                       cwd=str(self.repo), check=True, capture_output=True)

        binary_path.write_bytes(b"\x00\x01\x02\x03\x04\x05")
        subprocess.run(["git", "add", "binary.bin"], cwd=str(self.repo),
                       check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Modify binary"],
                       cwd=str(self.repo), check=True, capture_output=True)

        state = self.get("/api/state").get_json()
        result = self.get(f"/api/show?commit_hash={state['commits'][0]['commit_hash']}").get_json()
        self.assertTrue(result["ok"])


# ---------------------------------------------------------------------------
# Log robustness
# ---------------------------------------------------------------------------

@pytest.mark.release
class LogRobustnessTests(StandardAPITest):

    def setUp(self):
        super().setUp()
        self._log_file = self.tmpdir / "test.log"
        self.app = create_app(str(self.repo), TOKEN, log_path=self._log_file)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_append_log_handles_permission_denied(self):
        log_file = self.tmpdir / "readonly.log"
        log_file.touch()
        import stat
        log_file.chmod(0o444)

        try:
            app = create_app(str(self.repo), TOKEN, log_path=log_file)
            app.config["TESTING"] = True
            client = app.test_client()
            state_resp = client.get("/api/state", headers={"X-Token": TOKEN})
            state = state_resp.get_json()
            h = state["commits"][0]["commit_hash"]

            result = client.post("/api/rebase/reword", json={
                "commit_hashes": [h],
                "new_message": "Test message"
            }, headers={"X-Token": TOKEN}).get_json()
            self.assertTrue(result["ok"])
        finally:
            log_file.chmod(0o644)

    def test_append_log_creates_missing_file(self):
        log_file = self.tmpdir / "new.log"
        self.assertFalse(log_file.exists())

        app = create_app(str(self.repo), TOKEN, log_path=log_file)
        app.config["TESTING"] = True
        client = app.test_client()
        state = client.get("/api/state", headers={"X-Token": TOKEN}).get_json()
        h = state["commits"][0]["commit_hash"]
        client.post("/api/rebase/reword", json={
            "commit_hashes": [h],
            "new_message": "Test"
        }, headers={"X-Token": TOKEN})

        self.assertTrue(log_file.exists())


# ---------------------------------------------------------------------------
# Edge cases: reset to merge commits
# ---------------------------------------------------------------------------

@pytest.mark.release
class ResetMergeEndpointTests(StandardAPITest):

    def test_state_shows_merge_commits(self):
        state = self.get("/api/state").get_json()
        self.assertGreaterEqual(len(state["commits"]), 3)

    def test_reset_to_before_merge(self):
        state = self.get("/api/state").get_json()
        non_merge = next(c for c in state["commits"] if c["message"] != "Merge feature work")

        result = self.post("/api/reset", json={"commit_hash": non_merge["commit_hash"]}).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["commits"][0]["commit_hash"], non_merge["commit_hash"])


# ---------------------------------------------------------------------------
# _list_commits edge cases
# ---------------------------------------------------------------------------

@pytest.mark.release
class UnusualCommitMessageTests(StandardAPITest):
    """Test that _list_commits handles complicated-but-legal commit messages."""

    COMPLICATED_MESSAGE = (
        'Fix: handle edge\tcase\n\n'
        'This commit:\n'
        '\t- has "double" and \'single\' quotes\n'
        '\t- backslash C:\\path\\to\\file\n'
        '\t- unicode: café, naïve, 日本語 🎉\n\n'
        'Fixes #42'
    )

    def test_state_returns_unusual_message(self):
        state = self.get("/api/state").get_json()
        self.assertTrue(state["ok"])
        self.assertGreater(len(state["commits"]), 0)
        commit = next((c for c in state["commits"] if c["message"] == self.COMPLICATED_MESSAGE), None)
        self.assertIsNotNone(commit, "Unusual message commit not found in persistent repo")
        self.assertEqual(commit["message"], self.COMPLICATED_MESSAGE)

    def test_show_returns_unusual_message(self):
        state = self.get("/api/state").get_json()
        commit = next((c for c in state["commits"] if c["message"] == self.COMPLICATED_MESSAGE), None)
        self.assertIsNotNone(commit, "Unusual message commit not found in persistent repo")
        data = self.get(f"/api/show?commit_hash={commit['commit_hash']}").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commit"]["message"], self.COMPLICATED_MESSAGE)


# ---------------------------------------------------------------------------
# Undo/Redo backend methods
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
