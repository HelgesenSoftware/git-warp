from dataclasses import asdict
import hmac
import os
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, abort, Response

from git_history.backend import GitHistory, ErrorResponse, GitError, GitHistoryError, _LOG_PATH


def create_app(repo_path, token, log_path=None):
    _static = str(Path(__file__).parent / "static")
    app = Flask(__name__, static_folder=_static, static_url_path="/static")
    gh = GitHistory(repo_path, log_path=log_path or _LOG_PATH)

    def get_body():
        return request.get_json(silent=True) or {}

    def state_with_diff(state, select_index):
        # Bundle the selected commit's diff so a mutation is a single round-trip.
        # The client sends the post-op selection index: the new hash only exists
        # after the op runs, so the client cannot name it, and for a reorder the
        # selected commit is not derivable from the order alone. Best-effort —
        # a diff read failure must not fail the mutation.
        if isinstance(select_index, int) and not state.conflict and 0 <= select_index < len(state.commits):
            try:
                state.diff = asdict(gh.show(state.commits[select_index].commit_hash))
            except GitError:
                pass
        return jsonify(asdict(state))

    @app.before_request
    def auth():
        if request.path.startswith("/api/"):
            tok = request.headers.get("X-Token", "")
            if not hmac.compare_digest(tok, token):
                abort(403)

    @app.errorhandler(GitError)
    def handle_git_error(e):
        if isinstance(e, GitHistoryError):
            return jsonify(asdict(ErrorResponse(error=e.code, message=e.message)))
        if "cannot resolve commit" in str(e):
            return jsonify(asdict(ErrorResponse(error="invalid_commit", message=str(e))))
        return jsonify(asdict(ErrorResponse(error="git_failed", message=str(e))))

    @app.route("/")
    def index():
        return send_from_directory(_static, "index.html")

    @app.route("/manual")
    def manual():
        return send_from_directory(_static, "manual.html")

    @app.route("/api/state")
    def api_state():
        return jsonify(asdict(gh.read_state()))

    @app.route("/api/stash", methods=["POST"])
    def api_stash():
        return jsonify(asdict(gh.stash()))

    @app.route("/api/stash/pop", methods=["POST"])
    def api_stash_pop():
        return jsonify(asdict(gh.stash_pop()))

    @app.route("/api/rebase/move", methods=["POST"])
    def api_rebase_move():
        body = get_body()
        return state_with_diff(gh.move(body.get("order")), body.get("select_index"))

    @app.route("/api/rebase/squash", methods=["POST"])
    def api_rebase_squash():
        body = get_body()
        return state_with_diff(gh.squash(body.get("commit_hashes")), body.get("select_index"))

    @app.route("/api/rebase/fixup", methods=["POST"])
    def api_rebase_fixup():
        body = get_body()
        return state_with_diff(gh.fixup(body.get("commit_hashes")), body.get("select_index"))

    @app.route("/api/rebase/reword", methods=["POST"])
    def api_rebase_reword():
        body = get_body()
        hashes = body.get("commit_hashes") or []
        if not hashes:
            raise GitHistoryError("invalid_request")
        return state_with_diff(gh.reword(hashes[0], body.get("new_message")), body.get("select_index"))

    @app.route("/api/rebase/split", methods=["POST"])
    def api_rebase_split():
        body = get_body()
        return jsonify(asdict(gh.split(body.get("commit_hash", ""), body.get("files_to_split") or [])))

    @app.route("/api/rebase/continue", methods=["POST"])
    def api_rebase_continue():
        return jsonify(asdict(gh.rebase_continue()))

    @app.route("/api/rebase/abort", methods=["POST"])
    def api_rebase_abort():
        return jsonify(asdict(gh.rebase_abort()))

    @app.route("/api/reset", methods=["POST"])
    def api_reset():
        return jsonify(asdict(gh.reset(get_body().get("commit_hash", ""))))

    @app.route("/api/branch", methods=["POST"])
    def api_branch():
        body = get_body()
        return jsonify(asdict(gh.create_branch(body.get("branch_name", ""), body.get("commit_hash", ""))))

    @app.route("/api/branch", methods=["DELETE"])
    def api_branch_delete():
        return jsonify(asdict(gh.delete_branch(get_body().get("branch_name", ""))))

    @app.route("/api/submodule/update", methods=["POST"])
    def api_submodule_update():
        return jsonify(asdict(gh.submodule_update()))

    @app.route("/api/switch", methods=["POST"])
    def api_switch():
        body = get_body()
        return jsonify(asdict(gh.switch_branch(body.get("branch", ""), allow_different_gitmodules=bool(body.get("allow_different_gitmodules", False)))))

    @app.route("/api/show")
    def api_show():
        return jsonify(asdict(gh.show(request.args.get("commit_hash", ""))))

    @app.route("/log")
    def log_view():
        return Response(gh.read_log(), mimetype="text/plain")

    @app.route("/api/quit", methods=["POST"])
    def api_quit():
        response = jsonify({"ok": True})
        # os._exit skips atexit; safe because all temp files are unlinked in their own finally blocks
        response.call_on_close(lambda: os._exit(0))
        return response

    return app
