"""
Unit tests for the git-history REST API.

These tests exercise the Flask endpoints defined in the plan. They verify
HTTP methods, status codes, JSON structure, auth token enforcement, and that
each endpoint correctly delegates to the GitHistory backend.

The Flask app is expected to live in ``git_history.py`` and expose a
``create_app(repo_path, token)`` factory that returns a configured Flask app.
The app stores a ``GitHistory`` instance on ``app.config["GH"]`` and the auth
token on ``app.config["TOKEN"]``.

Endpoints under test:

    GET  /api/state                -> state JSON
    POST /api/stash                -> state | error JSON
    POST /api/stash/pop            -> state | error JSON
    POST /api/rebase               -> state | conflict | error JSON
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
from git_history.rest_api import create_app


TOKEN = "test-token-abc123"


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------

class StandardAPITest(unittest.TestCase):
    """Fresh clone of the 21-commit test repo with a Flask test client."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-history-api-test-"))
        self.repo = self.tmpdir / "repo"
        persistent_repo = _ensure_persistent_test_repo()
        subprocess.run(["git", "clone", str(persistent_repo), str(self.repo)],
                       capture_output=True, check=True)
        # Remove origin remote (clone sets it to persistent repo, but tests expect no remote)
        subprocess.run(["git", "remote", "remove", "origin"],
                       cwd=str(self.repo), capture_output=True)
        # Allow file:// URLs for submodule operations (same as persistent repo)
        subprocess.run(["git", "config", "protocol.file.allow", "always"],
                       cwd=str(self.repo), capture_output=True)
        # Ensure refs/heads/main reflog has at least one entry (not guaranteed after a fresh clone).
        subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=str(self.repo), capture_output=True)
        self.app = create_app(str(self.repo), TOKEN)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def get(self, url, **kwargs):
        return self.client.get(url, headers={"X-Token": TOKEN}, **kwargs)

    def post(self, url, json=None, **kwargs):
        return self.client.post(url, json=json,
                                headers={"X-Token": TOKEN}, **kwargs)

    def make_dirty(self, path="README.md", content=b"# changed\n"):
        (self.repo / path).write_bytes(content)

    def make_staged(self, path="README.md", content=b"# staged\n"):
        (self.repo / path).write_bytes(content)
        subprocess.run(["git", "add", path], cwd=str(self.repo), check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

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

    def test_api_state_with_token_in_query_string(self):
        resp = self.client.get(f"/api/state?t={TOKEN}")
        self.assertEqual(resp.status_code, 200)

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

    def test_returns_json(self):
        resp = self.get("/api/state")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content_type, "application/json")

    def test_state_has_expected_fields(self):
        data = self.get("/api/state").get_json()
        self.assertTrue(data["ok"])
        self.assertIn("branch", data)
        self.assertIn("dirty", data)
        self.assertIn("has_stash", data)
        self.assertIn("rebase_in_progress", data)
        self.assertIn("conflict_files", data)
        self.assertIn("commits", data)
        self.assertIn("branch_history", data)

    def test_state_commit_count(self):
        data = self.get("/api/state").get_json()
        self.assertEqual(len(data["commits"]), len(COMMITS))

    def test_state_commits_newest_first(self):
        data = self.get("/api/state").get_json()
        self.assertEqual(data["commits"][0]["message"], "Add CI workflow")
        self.assertEqual(data["commits"][-1]["message"], "Initial commit")

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

    def test_short_hash_is_seven_char_prefix(self):
        for c in self.get("/api/state").get_json()["commits"]:
            self.assertEqual(len(c["short_hash"]), 7)
            self.assertTrue(c["commit_hash"].startswith(c["short_hash"]))

    def test_branch_history_is_deduped_by_hash(self):
        hashes = [e["commit_hash"] for e in self.get("/api/state").get_json()["branch_history"]]
        self.assertEqual(len(hashes), len(set(hashes)))

    def test_branch_history_entries_have_label_and_timestamp(self):
        for entry in self.get("/api/state").get_json()["branch_history"]:
            self.assertIsNotNone(entry["commit_hash"])
            self.assertIsNotNone(entry["label"])
            self.assertIsNotNone(entry["timestamp"])

    def test_state_reports_dirty_tree(self):
        self.make_dirty()
        self.assertTrue(self.get("/api/state").get_json()["dirty"])

    def test_state_includes_reflog_expiry_field(self):
        data = self.get("/api/state").get_json()
        self.assertIn("reflog_expiry", data)
        self.assertIsInstance(data["reflog_expiry"], str)
        self.assertGreater(len(data["reflog_expiry"]), 0)

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
# POST /api/rebase — move
# ---------------------------------------------------------------------------

class RebaseMoveEndpointTests(StandardAPITest):

    def test_swap_two_newest(self):
        state = self.get("/api/state").get_json()
        commit_hashes = [c["commit_hash"] for c in state["commits"]]
        commit_hashes[0], commit_hashes[1] = commit_hashes[1], commit_hashes[0]

        data = self.post("/api/rebase", json={
            "operation": "move", "order": commit_hashes,
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commits"][0]["message"],
                         state["commits"][1]["message"])

    def test_move_unchanged_order_is_noop(self):
        state = self.get("/api/state").get_json()
        commit_hashes = [c["commit_hash"] for c in state["commits"]]

        data = self.post("/api/rebase", json={
            "operation": "move", "order": commit_hashes,
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

        data = self.post("/api/rebase", json={"operation": "move", "order": order}).get_json()
        self.assertTrue(data["ok"])
        msgs_after = [c["message"] for c in data["commits"]]
        self.assertEqual(msgs_after[5], msgs_before[0])
        self.assertEqual(sorted(msgs_after), sorted(msgs_before))

    def test_move_multiple_selected_commits(self):
        state = self.get("/api/state").get_json()
        msgs_before = [c["message"] for c in state["commits"]]
        hashes = [c["commit_hash"] for c in state["commits"]]
        new_order = hashes[:5] + hashes[7:9] + hashes[5:7] + hashes[9:]

        data = self.post("/api/rebase", json={"operation": "move", "order": new_order}).get_json()
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

        data = self.post("/api/rebase", json={
            "operation": "move", "order": commit_hashes,
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")

    @pytest.mark.release
    def test_rebase_uses_post(self):
        resp = self.get("/api/rebase")
        self.assertEqual(resp.status_code, 405)


# ---------------------------------------------------------------------------
# POST /api/rebase — squash
# ---------------------------------------------------------------------------

class RebaseSquashEndpointTests(StandardAPITest):

    def test_squash_two_adjacent(self):
        state = self.get("/api/state").get_json()
        commit_hashes = [state["commits"][0]["commit_hash"], state["commits"][1]["commit_hash"]]

        data = self.post("/api/rebase", json={
            "operation": "squash", "commit_hashes": commit_hashes,
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["commits"]), len(state["commits"]) - 1)

    @pytest.mark.release
    def test_squash_refused_when_dirty(self):
        self.make_dirty()
        state = self.get("/api/state").get_json()
        commit_hashes = [state["commits"][0]["commit_hash"], state["commits"][1]["commit_hash"]]

        data = self.post("/api/rebase", json={
            "operation": "squash", "commit_hashes": commit_hashes,
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")


# ---------------------------------------------------------------------------
# POST /api/rebase — fixup
# ---------------------------------------------------------------------------

class RebaseFixupEndpointTests(StandardAPITest):

    def test_fixup_middle_commit(self):
        state = self.get("/api/state").get_json()
        target = state["commits"][3]

        data = self.post("/api/rebase", json={
            "operation": "fixup", "commit_hashes": [target["commit_hash"]],
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["commits"]), len(state["commits"]) - 1)
        msgs = [c["message"] for c in data["commits"]]
        self.assertNotIn(target["message"], msgs)

    @pytest.mark.release
    def test_fixup_root_refused(self):
        state = self.get("/api/state").get_json()
        root = state["commits"][-1]["commit_hash"]

        data = self.post("/api/rebase", json={
            "operation": "fixup", "commit_hashes": [root],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    @pytest.mark.release
    def test_fixup_refused_when_dirty(self):
        self.make_dirty()
        h = self.get("/api/state").get_json()["commits"][3]["commit_hash"]
        data = self.post("/api/rebase", json={
            "operation": "fixup", "commit_hashes": [h],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")


# ---------------------------------------------------------------------------
# POST /api/rebase — reword
# ---------------------------------------------------------------------------

class RebaseRewordEndpointTests(StandardAPITest):

    def test_reword_top_commit(self):
        state = self.get("/api/state").get_json()
        h = state["commits"][0]["commit_hash"]

        data = self.post("/api/rebase", json={
            "operation": "reword",
            "commit_hashes": [h],
            "new_message": "Brand new message",
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commits"][0]["message"], "Brand new message")

    @pytest.mark.release
    def test_reword_missing_message_returns_error(self):
        state = self.get("/api/state").get_json()
        h = state["commits"][0]["commit_hash"]

        data = self.post("/api/rebase", json={
            "operation": "reword",
            "commit_hashes": [h],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    @pytest.mark.release
    def test_reword_refused_when_dirty(self):
        self.make_dirty()
        state = self.get("/api/state").get_json()
        h = state["commits"][0]["commit_hash"]

        data = self.post("/api/rebase", json={
            "operation": "reword",
            "commit_hashes":[h],
            "new_message": "x",
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")

    def test_reword_middle_commit(self):
        state = self.get("/api/state").get_json()
        h = state["commits"][5]["commit_hash"]
        data = self.post("/api/rebase", json={
            "operation": "reword", "commit_hashes": [h], "new_message": "Reworded middle",
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
        data = self.post("/api/rebase", json={
            "operation": "reword", "commit_hashes": [h], "new_message": new_msg,
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commits"][0]["message"], new_msg)


# ---------------------------------------------------------------------------
# POST /api/rebase — invalid operation
# ---------------------------------------------------------------------------

class RebaseInvalidTests(StandardAPITest):

    @pytest.mark.release
    def test_unknown_operation_returns_error(self):
        data = self.post("/api/rebase", json={
            "operation": "unknown",
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    @pytest.mark.release
    def test_missing_body_returns_error(self):
        data = self.post("/api/rebase").get_json()
        self.assertFalse(data["ok"])

    @pytest.mark.release
    def test_rebase_move_with_missing_order_field(self):
        data = self.post("/api/rebase", json={
            "operation": "move",
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


# ---------------------------------------------------------------------------
# Staged changes (index) row
# ---------------------------------------------------------------------------

class IndexRowTests(StandardAPITest):
    """Tests for the synthetic 'index' row shown when staged changes exist."""

    @pytest.mark.release
    def test_state_no_index_row_when_clean(self):
        state = self.get("/api/state").get_json()
        hashes = [c["commit_hash"] for c in state["commits"]]
        self.assertNotIn("index", hashes)

    def test_state_shows_index_row_when_staged(self):
        self.make_staged()
        state = self.get("/api/state").get_json()
        self.assertEqual(state["commits"][0]["commit_hash"], "index")

    @pytest.mark.release
    def test_index_row_label(self):
        self.make_staged()
        state = self.get("/api/state").get_json()
        index_commit = state["commits"][0]
        self.assertEqual(index_commit["short_hash"], "index")
        self.assertIn("Staged", index_commit["message"])

    @pytest.mark.release
    def test_index_row_disappears_after_unstage(self):
        self.make_staged()
        state = self.get("/api/state").get_json()
        self.assertEqual(state["commits"][0]["commit_hash"], "index")
        subprocess.run(["git", "restore", "--staged", "README.md"],
                       cwd=str(self.repo), check=True, capture_output=True)
        state2 = self.get("/api/state").get_json()
        hashes = [c["commit_hash"] for c in state2["commits"]]
        self.assertNotIn("index", hashes)

    def test_show_index_returns_diff(self):
        self.make_staged()
        data = self.get("/api/show?commit_hash=index").get_json()
        self.assertTrue(data["ok"])
        self.assertIn("diff --git", data["diff"])

    @pytest.mark.release
    def test_show_index_commit_fields(self):
        self.make_staged()
        data = self.get("/api/show?commit_hash=index").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commit"]["commit_hash"], "index")
        self.assertEqual(data["commit"]["short_hash"], "index")

    @pytest.mark.release
    def test_show_index_without_staged_changes_returns_empty_diff(self):
        data = self.get("/api/show?commit_hash=index").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["diff"], "")

    def test_rebase_move_ignores_index_hash(self):
        self.make_staged()
        state = self.get("/api/state").get_json()
        # Build order that excludes index; index should not be movable
        order = [c["commit_hash"] for c in state["commits"] if c["commit_hash"] != "index"]
        result = self.post("/api/rebase", json={"operation": "move", "order": order}).get_json()
        self.assertTrue(result["ok"])

    def test_rebase_squash_rejects_index(self):
        self.make_staged()
        state = self.get("/api/state").get_json()
        real_commits = [c["commit_hash"] for c in state["commits"] if c["commit_hash"] != "index"]
        result = self.post("/api/rebase", json={
            "operation": "squash",
            "commit_hashes": ["index", real_commits[0]],
        }).get_json()
        self.assertFalse(result["ok"])

    def test_rebase_fixup_rejects_index(self):
        self.make_staged()
        state = self.get("/api/state").get_json()
        real_commits = [c["commit_hash"] for c in state["commits"] if c["commit_hash"] != "index"]
        result = self.post("/api/rebase", json={
            "operation": "fixup",
            "commit_hashes": ["index", real_commits[0]],
        }).get_json()
        self.assertFalse(result["ok"])

    def test_rebase_reword_rejects_index(self):
        result = self.post("/api/rebase", json={
            "operation": "reword",
            "commit_hashes": ["index"],
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
        return self.post("/api/rebase", json={"operation": "move", "order": order})

    def test_move_produces_conflict(self):
        data = self._swap_order().get_json()
        self.assertFalse(data["ok"])
        self.assertTrue(data.get("conflict"))
        self.assertIn("README.md", data["conflict_files"])
        self.assertTrue(data["rebase_in_progress"])

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

    def test_conflict_response_includes_full_state(self):
        data = self._swap_order().get_json()
        self.assertTrue(data.get("conflict"))
        # Conflict response must carry the full state fields, not just conflict
        # info, so the frontend can refresh uniformly.
        for field in ("branch", "branches", "commits", "branch_history",
                      "dirty", "has_stash", "submodule_update_suggested"):
            self.assertIn(field, data)
        self.assertTrue(data["commits"])

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

    def test_quit_returns_ok(self):
        with patch("git_history.os._exit"):
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
        self.assertIn(b"git-history", resp.data)

    def test_manual_requires_no_token(self):
        resp = self.client.get("/manual")
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# POST /api/rebase — consecutive commit validation
# ---------------------------------------------------------------------------

class RebaseConsecutiveEndpointTests(StandardAPITest):

    @pytest.mark.release
    def test_squash_non_adjacent_commits_returns_invalid_request(self):
        state = self.get("/api/state").get_json()
        h0 = state["commits"][0]["commit_hash"]
        h2 = state["commits"][2]["commit_hash"]

        data = self.post("/api/rebase", json={
            "operation": "squash",
            "commit_hashes": [h0, h2],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    @pytest.mark.release
    def test_fixup_non_adjacent_commits_returns_invalid_request(self):
        state = self.get("/api/state").get_json()
        h0 = state["commits"][0]["commit_hash"]
        h2 = state["commits"][2]["commit_hash"]

        data = self.post("/api/rebase", json={
            "operation": "fixup",
            "commit_hashes": [h0, h2],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    @pytest.mark.release
    def test_squash_non_adjacent_does_not_modify_commit_history(self):
        state = self.get("/api/state").get_json()
        hashes_before = [c["commit_hash"] for c in state["commits"]]
        self.post("/api/rebase", json={"operation": "squash", "commit_hashes": [hashes_before[0], hashes_before[3]]})
        hashes_after = [c["commit_hash"] for c in self.get("/api/state").get_json()["commits"]]
        self.assertEqual(hashes_after, hashes_before)

    def test_squash_three_adjacent_commits_is_valid(self):
        state = self.get("/api/state").get_json()
        hashes = [state["commits"][i]["commit_hash"] for i in range(3)]
        data = self.post("/api/rebase", json={"operation": "squash", "commit_hashes": hashes}).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["commits"]), len(state["commits"]) - 2)

    @pytest.mark.release
    def test_squash_non_consecutive_rejects_and_preserves_head(self):
        state = self.get("/api/state").get_json()
        initial_head = state["commits"][0]["commit_hash"]
        h0 = state["commits"][0]["commit_hash"]
        h2 = state["commits"][2]["commit_hash"]

        data = self.post("/api/rebase", json={
            "operation": "squash",
            "commit_hashes": [h0, h2],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")
        state_after = self.get("/api/state").get_json()
        self.assertEqual(state_after["commits"][0]["commit_hash"], initial_head)


# ---------------------------------------------------------------------------
# POST /api/submodule/update
# ---------------------------------------------------------------------------

@pytest.mark.release
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
        self.post("/api/rebase", json={"operation": "move", "order": order})
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
        self.post("/api/rebase", json={
            "operation": "reword",
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
        # Query string token should work
        resp = self.client.get(f"/api/state?t={TOKEN}")
        self.assertEqual(resp.status_code, 200)

    @pytest.mark.release
    def test_wrong_query_token_returns_403(self):
        resp = self.client.get("/api/state?t=wrong")
        self.assertEqual(resp.status_code, 403)

    @pytest.mark.release
    def test_malformed_json_with_valid_token(self):
        resp = self.client.post("/api/rebase",
                                data="{invalid json}",
                                headers={"X-Token": TOKEN,
                                        "Content-Type": "application/json"})
        # Should fail on JSON parsing, not auth
        self.assertNotEqual(resp.status_code, 403)

    @pytest.mark.release
    def test_missing_operation_field_returns_error(self):
        data = self.post("/api/rebase", json={}).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")


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
        resp = self.get("/api/rebase")
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
        data = self.post("/api/rebase", json={
            "operation": "nonexistent"
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    def test_missing_required_field_error_response(self):
        state = self.get("/api/state").get_json()
        data = self.post("/api/rebase", json={
            "operation": "move"
            # Missing "order" field
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")


# ---------------------------------------------------------------------------
# Branch history labels
# ---------------------------------------------------------------------------

class BranchHistoryLabelTests(StandardAPITest):

    def test_reword_produces_reword_entry(self):
        state = self.get("/api/state").get_json()
        h = state["commits"][0]["commit_hash"]
        data = self.post("/api/rebase", json={
            "operation": "reword", "commit_hashes": [h], "new_message": "Rewarded top",
        }).get_json()
        self.assertTrue(data["ok"])
        entries = [e for e in data["branch_history"] if e["label"].startswith("reword:")]
        self.assertEqual(len(entries), 1)
        self.assertIn("Rewarded top", entries[0]["label"])

    def test_fixup_produces_fixup_entry(self):
        state = self.get("/api/state").get_json()
        absorbed_msg = state["commits"][0]["message"]
        data = self.post("/api/rebase", json={
            "operation": "fixup", "commit_hashes": [state["commits"][0]["commit_hash"]],
        }).get_json()
        self.assertTrue(data["ok"])
        entries = [e for e in data["branch_history"] if e["label"].startswith("fixup:")]
        self.assertEqual(len(entries), 1)
        self.assertIn(absorbed_msg.split("\n")[0], entries[0]["label"])

    def test_move_produces_reorder_entry(self):
        state = self.get("/api/state").get_json()
        order = [c["commit_hash"] for c in state["commits"]]
        order[0], order[1] = order[1], order[0]
        data = self.post("/api/rebase", json={"operation": "move", "order": order}).get_json()
        self.assertTrue(data["ok"])
        entries = [e for e in data["branch_history"] if e["label"].startswith("reorder")]
        self.assertEqual(len(entries), 1)

    def test_squash_produces_squash_entry(self):
        state = self.get("/api/state").get_json()
        absorbed_msg = state["commits"][0]["message"]
        hashes = [state["commits"][0]["commit_hash"], state["commits"][1]["commit_hash"]]
        data = self.post("/api/rebase", json={"operation": "squash", "commit_hashes": hashes}).get_json()
        self.assertTrue(data["ok"])
        entries = [e for e in data["branch_history"] if e["label"].startswith("squash:")]
        self.assertEqual(len(entries), 1)
        self.assertIn(absorbed_msg.split("\n")[0], entries[0]["label"])


    def test_mixed_operation_rebase_produces_generic_rebase_label(self):
        """Mixed rebase (reword + fixup) done outside git-history should label as 'rebase'."""
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
        editor_py = REPO_ROOT / "git_history" / "editor.py"
        editor_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(editor_py))}"
        env = os.environ.copy()
        env["GIT_SEQUENCE_EDITOR"] = editor_cmd
        env["GIT_EDITOR"] = editor_cmd
        env["GIT_HISTORY_TODO"] = str(todo_path)
        env["GIT_HISTORY_MSG"] = str(msg_path)
        # Run the mixed rebase outside git-history
        result = subprocess.run(
            ["git", "rebase", "-i", h2],
            cwd=str(self.repo),
            env=env,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, f"rebase failed: {result.stderr.decode()}")
        # Fetch state and verify the mixed rebase is labeled "rebase"
        state = self.get("/api/state").get_json()
        entries = [e for e in state["branch_history"] if e["label"] == "rebase"]
        self.assertGreater(len(entries), 0, "Expected a 'rebase' label for mixed operations (reword + fixup)")

    def test_reset_produces_reset_label_with_message(self):
        state = self.get("/api/state").get_json()
        target = state["commits"][3]
        data = self.post("/api/reset", json={"commit_hash": target["commit_hash"]}).get_json()
        self.assertTrue(data["ok"])
        entries = [e for e in data["branch_history"] if e["label"].startswith("reset:")]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["label"], f"reset: {target['message'].split(chr(10))[0]}")
        self.assertNotIn("\n", entries[0]["label"])


# ---------------------------------------------------------------------------
# Sequential operations
# ---------------------------------------------------------------------------

class SequentialOpsTests(StandardAPITest):

    def test_multiple_squash_operations_in_sequence(self):
        state = self.get("/api/state").get_json()
        hashes = [c["commit_hash"] for c in state["commits"]]
        r1 = self.post("/api/rebase", json={"operation": "squash", "commit_hashes": [hashes[0], hashes[1]]}).get_json()
        self.assertTrue(r1["ok"])
        r2 = self.post("/api/rebase", json={"operation": "squash", "commit_hashes": [r1["commits"][0]["commit_hash"], r1["commits"][1]["commit_hash"]]}).get_json()
        self.assertTrue(r2["ok"])
        self.assertEqual(len(r2["commits"]), len(r1["commits"]) - 1)

    def test_reword_then_move(self):
        state = self.get("/api/state").get_json()
        h0 = state["commits"][0]["commit_hash"]
        r1 = self.post("/api/rebase", json={"operation": "reword", "commit_hashes": [h0], "new_message": "Reworded"}).get_json()
        self.assertTrue(r1["ok"])
        order = [c["commit_hash"] for c in r1["commits"]]
        order[0], order[1] = order[1], order[0]
        r2 = self.post("/api/rebase", json={"operation": "move", "order": order}).get_json()
        self.assertTrue(r2["ok"])

    def test_move_then_squash_then_reset(self):
        state = self.get("/api/state").get_json()
        orig_hashes = [c["commit_hash"] for c in state["commits"]]
        order = orig_hashes[:]
        order[0], order[1] = order[1], order[0]
        r1 = self.post("/api/rebase", json={"operation": "move", "order": order}).get_json()
        self.assertTrue(r1["ok"])
        r2 = self.post("/api/rebase", json={"operation": "squash", "commit_hashes": [r1["commits"][0]["commit_hash"], r1["commits"][1]["commit_hash"]]}).get_json()
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
        data = self.post("/api/rebase", json={"operation": "move", "order": order}).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commits"][1]["message"], "Add complex filename")

    def test_squash_and_show_with_complex_filename(self):
        filename = "file with spaces (日本語) [draft].txt"
        (self.repo / filename).write_bytes("content\n".encode("utf-8"))
        subprocess.run(["git", "add", filename], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add Japanese file"], cwd=str(self.repo), check=True, capture_output=True)

        state = self.get("/api/state").get_json()
        hashes = [state["commits"][0]["commit_hash"], state["commits"][1]["commit_hash"]]
        r1 = self.post("/api/rebase", json={"operation": "squash", "commit_hashes": hashes}).get_json()
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
        subprocess.run(
            ["git", "branch", "feature"],
            cwd=str(self.repo), check=True, capture_output=True,
        )

    def test_state_includes_branches_list(self):
        state = self.get("/api/state").get_json()
        self.assertIn("main", state["branches"])

    def test_branches_list_includes_all_local_branches(self):
        state = self.get("/api/state").get_json()
        self.assertIn("feature", state["branches"])

    def test_switch_to_existing_branch(self):
        result = self.post("/api/switch", json={"branch": "feature"}).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["branch"], "feature")

    def test_switch_returns_full_state(self):
        result = self.post("/api/switch", json={"branch": "feature"}).get_json()
        self.assertIsNotNone(result["commits"])
        self.assertIsNotNone(result["branch_history"])
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
        self.post("/api/rebase", json={"operation": "move", "order": order})

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
# Pushed status
# ---------------------------------------------------------------------------

class PushedEndpointTests(StandardAPITest):

    def _add_remote_and_push(self):
        bare = self.tmpdir / "bare"
        bare.mkdir()
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
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-history-empty-api-"))
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
        self.assertEqual(state["branch_history"], [])
        self.assertFalse(state["dirty"])

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

            result = client.post("/api/rebase", json={
                "operation": "reword",
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
        client.post("/api/rebase", json={
            "operation": "reword",
            "commit_hashes": [h],
            "new_message": "Test"
        }, headers={"X-Token": TOKEN})

        self.assertTrue(log_file.exists())


# ---------------------------------------------------------------------------
# Edge cases: reset to merge commits
# ---------------------------------------------------------------------------

@pytest.mark.release
class ResetMergeEndpointTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-history-merge-api-"))
        self.repo = self.tmpdir / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(self.repo),
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"],
                       cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=str(self.repo), check=True, capture_output=True)

        _commit_raw(self.repo, "file.txt", b"base\n", "base", "alice", 0)

        subprocess.run(["git", "checkout", "-b", "feature"],
                       cwd=str(self.repo), check=True, capture_output=True)
        _commit_raw(self.repo, "feature.txt", b"feature\n", "feature work", "bob", 1)

        main_branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(self.repo), check=True, capture_output=True, text=True
        ).stdout.strip()
        subprocess.run(["git", "checkout", main_branch],
                       cwd=str(self.repo), check=True, capture_output=True)
        _commit_raw(self.repo, "main.txt", b"main\n", "main work", "carol", 2)

        subprocess.run(["git", "merge", "feature", "-m", "Merge feature"],
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

    def test_state_shows_merge_commits(self):
        state = self.get("/api/state").get_json()
        self.assertGreaterEqual(len(state["commits"]), 3)

    def test_reset_to_before_merge(self):
        state = self.get("/api/state").get_json()
        non_merge = next(c for c in state["commits"] if c["message"] != "Merge feature")

        result = self.post("/api/reset", json={"commit_hash": non_merge["commit_hash"]}).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["commits"][0]["commit_hash"], non_merge["commit_hash"])


# ---------------------------------------------------------------------------
# Undo/Redo backend methods
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
