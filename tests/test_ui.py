"""
UI regression tests using Playwright against a live Flask server.

Requires: pip install pytest-playwright && playwright install chromium
Run with: python -m pytest tests/test_ui.py -v
"""
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_history.rest_api import create_app
from conftest import _ensure_persistent_test_repo

TOKEN = "test-ui-token-abcdefgh12345678"


def _setup_ui_server(tmp_path_factory, prefix):
    """Set up a live git-history server for UI testing."""
    work = tmp_path_factory.mktemp(prefix)
    repo = work / "repo"
    template = _ensure_persistent_test_repo()
    subprocess.run(["git", "clone", str(template), str(repo)],
                   capture_output=True, check=True)
    subprocess.run(["git", "remote", "remove", "origin"], cwd=str(repo),
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "protocol.file.allow", "always"], cwd=str(repo),
                   capture_output=True, check=True)
    subprocess.run(["git", "submodule", "update", "--init", "--recursive"], cwd=str(repo),
                   capture_output=True, check=True)
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=str(repo),
                   capture_output=True, check=True)
    app = create_app(str(repo), TOKEN)
    server = make_server("127.0.0.1", 0, app)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{port}/?t={TOKEN}", "repo": repo}
    server.shutdown()


@pytest.fixture
def live_server(tmp_path_factory):
    yield from _setup_ui_server(tmp_path_factory, "ui-work")


@pytest.fixture
def drag_server(tmp_path_factory):
    yield from _setup_ui_server(tmp_path_factory, "ui-drag")


@pytest.fixture
def conflict_server(tmp_path_factory):
    yield from _setup_ui_server(tmp_path_factory, "ui-conflict")


def _row(page, message):
    return page.locator(f'.commit-row[data-message="{message}"]')


def drag_row(page, source_locator, target_locator, position="above"):
    """Simulate a mousedown/mousemove/mouseup drag using the row's drag handle."""
    try:
        # Ensure elements are visible and scrolled into view
        source_locator.scroll_into_view_if_needed()
        target_locator.scroll_into_view_if_needed()

        source_visible = source_locator.is_visible()
        target_visible = target_locator.is_visible()
        if not source_visible or not target_visible:
            raise RuntimeError(f"Locators not visible: source={source_visible}, target={target_visible}")

        handle = source_locator.locator(".drag-handle")
        src = handle.bounding_box()
        if not src:
            raise RuntimeError(f"Source handle bounding box is None")

        sx = src["x"] + src["width"] / 2
        sy = src["y"] + src["height"] / 2
        page.mouse.move(sx, sy)
        page.mouse.down()
        page.mouse.move(sx, sy + 3)  # small move to trigger onDragMove

        tgt = target_locator.bounding_box()
        if not tgt:
            raise RuntimeError(f"Target bounding box is None")

        if position == "above":
            dy = tgt["y"] + 2
        else:
            dy = tgt["y"] + tgt["height"] - 2
        page.mouse.move(tgt["x"] + tgt["width"] / 2, dy)
        page.mouse.up()
    except Exception as e:
        print(f"ERROR in drag_row: {e}")
        raise


def drag_row_and_wait(page, source_locator, target_locator, position="above"):
    response_received = []
    all_responses = []

    def capture_response(response):
        all_responses.append(response.url)
        if "/api/rebase" in response.url:
            response_received.append(response)

    # Set up listener before drag
    page.on("response", capture_response)
    drag_row(page, source_locator, target_locator, position)

    # Wait up to 15 seconds for response to be captured
    for _ in range(150):
        if response_received:
            break
        page.wait_for_timeout(100)

    # Remove listener after we're done with this drag
    page.remove_listener("response", capture_response)

    if not response_received:
        # Debug: show what responses we got
        api_responses = [r for r in all_responses if "/api/" in r]
        print(f"DEBUG: Got {len(all_responses)} responses total, {len(api_responses)} API responses")
        print(f"DEBUG: API responses: {api_responses}")
        raise TimeoutError(f"No /api/rebase response received within 15 seconds (got {len(api_responses)} API calls)")


def _selected_idx(page):
    return page.evaluate("""() => {
        const rows = Array.from(document.querySelectorAll('.commit-row'));
        const sel = document.querySelector('.commit-row.selected');
        return sel ? rows.indexOf(sel) : -1;
    }""")


def test_all_ui(page, live_server, drag_server, conflict_server):
    # Live server tests (all use the same page, continuous mutations)
    page.goto(live_server["url"])
    page.wait_for_selector(".commit-row")

    # Section 1: Basic page load and initial state
    assert page.locator(".commit-row").count() == 26
    assert page.locator(".branch-history-row").count() > 0

    # Section 2: Selection and action bar
    assert not page.locator("#btn-squash").is_visible()
    page.locator(".commit-row").nth(0).click()
    assert not page.locator("#btn-squash").is_visible()
    page.locator(".commit-row").nth(1).click(modifiers=["Shift"])
    assert page.locator("#btn-squash").is_visible()

    # Section 3: Fixup button disabled on oldest
    oldest = page.locator(".commit-row").last
    oldest.hover()
    assert oldest.locator("button[title='Fixup']").is_disabled()

    # Section 4: Inline reword via toolbar button
    page.locator(".commit-row").nth(0).click()
    page.locator("#btn-reword").click()
    ta = page.locator(".reword-input")
    ta.wait_for()
    ta.fill("reworded message")
    ta.press("Control+Enter")
    page.wait_for_function("!document.querySelector('.reword-input')")
    assert page.locator(".commit-row").nth(0).locator(".message").inner_text() == "reworded message"

    # Section 5: Branch history group consecutive rebases
    for msg in ["first reword", "second reword"]:
        page.locator(".commit-row").nth(0).click()
        page.locator("#btn-reword").click()
        ta = page.locator(".reword-input")
        ta.wait_for()
        ta.fill(msg)
        ta.press("Control+Enter")
        page.wait_for_function("!document.querySelector('.reword-input')")

    # Section 6: Fixup preserves selection (26 → 25)
    count_before = page.locator(".commit-row").count()
    page.locator(".commit-row").nth(2).hover()
    page.locator(".commit-row").nth(2).locator("button[title='Fixup']").click()
    page.wait_for_function(f"document.querySelectorAll('.commit-row').length === {count_before - 1}")
    assert _selected_idx(page) == 1

    # Section 7: Squash preserves selection (25 → 24)
    count_before = page.locator(".commit-row").count()
    page.locator(".commit-row").nth(2).click()
    page.locator(".commit-row").nth(3).click(modifiers=["Shift"])
    with page.expect_response("**/api/rebase"):
        page.locator("#btn-squash").click()
    page.wait_for_function(f"document.querySelectorAll('.commit-row').length === {count_before - 1}")
    assert _selected_idx(page) == 2

    # Section 8: Reword preserves selection (24 still)
    page.locator(".commit-row").nth(3).click()
    page.locator("#btn-reword").click()
    ta = page.locator(".reword-input")
    ta.wait_for()
    ta.fill("reworded message 2")
    with page.expect_response("**/api/rebase"):
        ta.press("Control+Enter")
    page.wait_for_function("!document.querySelector('.reword-input')")
    assert _selected_idx(page) == 3

    # Section 9: Squash (24 → 23)
    count_before = page.locator(".commit-row").count()
    page.locator(".commit-row").nth(0).click()
    page.locator(".commit-row").nth(1).click(modifiers=["Shift"])
    page.locator("#btn-squash").click()
    page.wait_for_function(f"document.querySelectorAll('.commit-row').length === {count_before - 1}")
    assert page.locator(".commit-row").count() == count_before - 1

    # Section 10: Fixup button (23 → 22)
    count_before = page.locator(".commit-row").count()
    row = page.locator(".commit-row").nth(1)
    row.hover()
    row.locator("button[title='Fixup']").click()
    page.wait_for_function(f"document.querySelectorAll('.commit-row').length === {count_before - 1}")
    assert page.locator(".commit-row").count() == count_before - 1

    # Section 11: Dblclick on non-HEAD commit resets branch to that commit
    count_before = page.locator(".commit-row").count()
    target_hash = page.locator(".commit-row").nth(2).get_attribute("data-commit-hash")
    with page.expect_response("**/api/reset"):
        page.locator(".commit-row").nth(2).dblclick()
    page.wait_for_function(f"document.querySelectorAll('.commit-row').length === {count_before - 2}")
    assert page.locator(".commit-row").nth(0).get_attribute("data-commit-hash") == target_hash
    # Confirm submodule update if the reset triggered the modal
    if page.evaluate("() => !document.getElementById('submodule-modal').classList.contains('hidden')"):
        with page.expect_response("**/api/submodule/update"):
            page.locator("#btn-submodule-ok").click()
        page.wait_for_function("document.getElementById('submodule-modal').classList.contains('hidden')")

    # Section 12: Dblclick on HEAD commit does not reset
    head_hash = page.locator(".commit-row").nth(0).get_attribute("data-commit-hash")
    count_before = page.locator(".commit-row").count()
    page.locator(".commit-row").nth(0).dblclick()
    page.wait_for_timeout(400)
    assert page.locator(".commit-row").count() == count_before
    assert page.locator(".commit-row").nth(0).get_attribute("data-commit-hash") == head_hash

    # Section 13: Stash button when dirty and stash/pop combined (end of live_server tests)
    assert not page.locator("#btn-stash").is_visible()
    (live_server["repo"] / "README.md").write_text("modified")
    page.locator("#btn-refresh").click()
    page.wait_for_selector("#btn-stash", state="visible")
    assert page.locator("#btn-stash").is_visible()
    page.locator("#btn-stash").click()
    page.wait_for_selector("#btn-stash-pop", state="visible")
    assert page.locator("#btn-stash-pop").is_visible()
    page.locator("#btn-stash-pop").click()
    page.wait_for_selector("#btn-stash", state="visible")
    assert page.locator("#btn-stash").is_visible()

    # Drag server tests (new page via goto)
    page.goto(drag_server["url"])
    page.wait_for_selector(".commit-row")

    # Section 12: Drag to same position is noop (non-mutating, run first)
    original = [r.get_attribute("data-commit-hash")
                for r in page.locator(".commit-row").all()]
    drag_row(page,
        page.locator(".commit-row").nth(0),
        page.locator(".commit-row").nth(0),
        "above")
    page.wait_for_timeout(300)
    result = [r.get_attribute("data-commit-hash")
              for r in page.locator(".commit-row").all()]
    assert result == original

    # Section 13: Mid-drag visual state — drop indicator, data-dragging attribute, dragged-row opacity.
    # Small +3px move only: triggers onDragMove (creates indicator) but keeps DOM order unchanged
    # so mouse.up does not fire a rebase that would leak state into Section 15.
    handle = page.locator(".commit-row").nth(0).locator(".drag-handle")
    src = handle.bounding_box()
    page.mouse.move(src["x"] + src["width"] / 2, src["y"] + src["height"] / 2)
    page.mouse.down()
    page.mouse.move(src["x"] + src["width"] / 2, src["y"] + src["height"] / 2 + 3)
    assert page.locator(".drop-indicator").count() > 0
    assert page.locator("#commits-list[data-dragging]").count() == 1
    assert page.evaluate("document.querySelector('.commit-row.dragging').style.opacity") == "0.4"
    page.mouse.up()

    # Reload to reset state for mutating tests
    page.reload()
    page.wait_for_selector(".commit-row")

    # Section 15: Drag reorder step commits (must run first due to state dependency)
    drag_row_and_wait(page,
        _row(page, "Step 5/5: Add settings page"),
        _row(page, "Step 3/5: Add search page"),
        "above")
    drag_row_and_wait(page,
        _row(page, "Step 4/5: Add contact page"),
        _row(page, "Step 3/5: Add search page"),
        "above")
    all_messages = [r.get_attribute("data-message")
                    for r in page.locator(".commit-row").all()]
    step_messages = [m for m in all_messages if m and m.startswith("Step")]
    assert step_messages == [
        "Step 5/5: Add settings page",
        "Step 4/5: Add contact page",
        "Step 3/5: Add search page",
        "Step 2/5: Add about page",
        "Step 1/5: Create homepage",
    ]

    # Section 16: Drag group of selected commits
    # Reload to get fresh state for this test (matches backend test's fresh state)
    page.reload()
    page.wait_for_selector(".commit-row")

    # Get initial messages at indices 5 and 6 (like backend test)
    msgs_before = [r.get_attribute("data-message")
                   for r in page.locator(".commit-row").all()]
    msg5_before = msgs_before[5]
    msg6_before = msgs_before[6]

    # Select both commits using click and Shift+click (indices 5 and 6)
    page.locator(".commit-row").nth(5).click()
    page.locator(".commit-row").nth(6).click(modifiers=["Shift"])

    # Drag the selected group to below row 8 (moving them to after index 8, like backend test)
    drag_row_and_wait(page,
        page.locator(".commit-row").nth(5),
        page.locator(".commit-row").nth(8),
        "below")

    # Get final messages
    msgs_after = [r.get_attribute("data-message")
                  for r in page.locator(".commit-row").all()]

    # Original commits 5 and 6 should now be at indices > 6 (like backend test)
    assert msgs_after.index(msg5_before) > 6
    assert msgs_after.index(msg6_before) > 6

    # Same set of messages, just reordered (like backend test)
    assert sorted(msgs_after) == sorted(msgs_before)

    # Section 17: Drag preserves selection
    # Reload to get fresh state for this test
    page.reload()
    page.wait_for_selector(".commit-row")
    msg_at_0 = page.locator(".commit-row").nth(0).get_attribute("data-message")
    drag_row_and_wait(page,
        page.locator(".commit-row").nth(0),
        page.locator(".commit-row").nth(3),
        "below")
    # Selection should be preserved on the commit we dragged
    selected_msg = page.evaluate("""() => {
        const sel = document.querySelector('.commit-row.selected');
        return sel ? sel.getAttribute('data-message') : null;
    }""")
    assert selected_msg == msg_at_0

    # Conflict server tests (new page via goto)
    page.goto(conflict_server["url"])
    page.wait_for_selector(".commit-row")
    initial_count = page.locator(".commit-row").count()

    # Section 18: Conflict modal appears
    drag_row(page, _row(page, "conflict: version A"), _row(page, "conflict: version B"), position="above")
    page.wait_for_selector("#conflict-modal:not(.hidden)")
    assert page.locator("#conflict-files li").count() > 0

    # Section 19: Conflict abort (continue from conflict state, don't re-drag)
    page.locator("#btn-abort").click()
    page.wait_for_selector("#conflict-modal", state="hidden")
    assert page.locator(".commit-row").count() == initial_count
