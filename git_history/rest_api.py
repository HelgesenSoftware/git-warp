from dataclasses import asdict
import hmac
import os
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, abort, Response

from git_history.backend import GitHistory, ErrorResponse, _LOG_PATH


def create_app(repo_path, token, log_path=None):
    _static = str(Path(__file__).parent / "static")
    app = Flask(__name__, static_folder=_static, static_url_path="/static")
    gh = GitHistory(repo_path, log_path=log_path or _LOG_PATH)
    app.config["GH"] = gh
    app.config["TOKEN"] = token

    def get_body():
        return request.get_json(silent=True) or {}

    @app.before_request
    def auth():
        if request.path.startswith("/api/"):
            tok = request.headers.get("X-Token", "") or request.args.get("t", "")
            if not hmac.compare_digest(tok, app.config["TOKEN"]):
                abort(403)

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

    @app.route("/api/rebase", methods=["POST"])
    def api_rebase():
        body = get_body()
        op = body.get("operation")
        if op == "move":
            result = gh.move(body.get("order"))
        elif op == "squash":
            result = gh.squash(body.get("commit_hashes"))
        elif op == "fixup":
            result = gh.fixup(body.get("commit_hashes"))
        elif op == "reword":
            hashes = body.get("commit_hashes") or []
            if not hashes:
                result = ErrorResponse(error="invalid_request")
            else:
                result = gh.reword(hashes[0], body.get("new_message"))
        else:
            result = ErrorResponse(error="invalid_request")
        return jsonify(asdict(result))

    @app.route("/api/rebase/continue", methods=["POST"])
    def api_rebase_continue():
        return jsonify(asdict(gh.rebase_continue()))

    @app.route("/api/rebase/abort", methods=["POST"])
    def api_rebase_abort():
        return jsonify(asdict(gh.rebase_abort()))

    @app.route("/api/reset", methods=["POST"])
    def api_reset():
        return jsonify(asdict(gh.reset(get_body().get("commit_hash", ""))))

    @app.route("/api/submodule/update", methods=["POST"])
    def api_submodule_update():
        return jsonify(asdict(gh.submodule_update()))

    @app.route("/api/switch", methods=["POST"])
    def api_switch():
        return jsonify(asdict(gh.switch_branch(get_body().get("branch", ""))))

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
