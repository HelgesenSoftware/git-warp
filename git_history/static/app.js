/* git-history front-end */
(function () {
  "use strict";

  // ---- Token ----
  const params = new URLSearchParams(window.location.search);
  const urlToken = params.get("t");
  if (urlToken) {
    localStorage.setItem("git_history_token", urlToken);
    history.replaceState(null, "", "/");
  }
  const TOKEN = localStorage.getItem("git_history_token") || "";
  if (params.get("dark") === "1") document.body.classList.add("dark");
  document.getElementById("log-link").href = "/log";

  // ---- State ----
  const INDEX_HASH = "index";
  let state = null;         // latest server state
  let selected = new Set(); // set of selected hashes
  let anchor = null;        // shift-click anchor hash
  let busy = false;
  let dragState = null;     // {hashes, fromIndex, placeholder}
  let branchHistoryEntries = [];   // current filtered branch history list
  let headBranchHistoryIdx = -1;   // index of HEAD in branchHistoryEntries

  // ---- DOM refs ----
  const $banner      = document.getElementById("banner");
  const $spinner     = document.getElementById("spinner");
  const $btnUndo     = document.getElementById("btn-undo");
  const $btnRedo     = document.getElementById("btn-redo");
  const $btnReword   = document.getElementById("btn-reword");
  const $btnStash    = document.getElementById("btn-stash");
  const $btnStashPop = document.getElementById("btn-stash-pop");
  const $btnRefresh  = document.getElementById("btn-refresh");
  const $btnSquash   = document.getElementById("btn-squash");
  const $btnQuit     = document.getElementById("btn-quit");
  const $commitsList = document.getElementById("commits-list");
  const $branchHistoryList  = document.getElementById("branch-history-list");
  const $branchHistoryTitle  = document.getElementById("branch-history-title-text");
  const $conflictModal = document.getElementById("conflict-modal");
  const $submoduleModal = document.getElementById("submodule-modal");
  const $conflictFiles = document.getElementById("conflict-files");
  const $btnAbort    = document.getElementById("btn-abort");
  const $btnContinue = document.getElementById("btn-continue");
  const $commitsHelpModal = document.getElementById("commits-help-modal");
  const $branchHistoryHelpModal = document.getElementById("branch-history-help-modal");
  const $diffPane      = document.getElementById("diff-pane");
  const $diffResize    = document.getElementById("diff-resize");
  const $diffFiles     = document.getElementById("diff-files");
  const $diffContent   = document.getElementById("diff-content");
  const $branchSelect  = document.getElementById("branch-select");

  // ---- API helpers ----
  function headers(extra) {
    return Object.assign({"X-Token": TOKEN}, extra || {});
  }

  async function api(method, url, body) {
    const opts = {method, headers: headers()};
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  function apiGet(url)        { return api("GET", url); }
  function apiPost(url, body) { return api("POST", url, body); }

  async function withSpinner(fn) {
    if (busy) return;
    showSpinner();
    try { return await fn(); }
    catch (err) { showBanner("Request failed: " + err.message, "error"); }
    finally { hideSpinner(); }
  }

  // ---- Spinner ----
  function showSpinner() { busy = true; $spinner.classList.remove("hidden"); $branchSelect.disabled = true; }
  function hideSpinner() { busy = false; $spinner.classList.add("hidden"); $branchSelect.disabled = !!(state && (state.dirty || state.rebase_in_progress)); }

  // ---- Banner ----
  function showBanner(msg, type) {
    $banner.textContent = msg;
    $banner.className = type; // "error" or "warning"
  }
  function clearBanner() { $banner.className = ""; $banner.textContent = ""; }

  // ---- Render ----
  function render() {
    if (!state) return;
    $branchHistoryTitle.textContent = "Branch History (Last " + state.reflog_expiry + ")";

    // Branch dropdown
    $branchSelect.innerHTML = "";
    if (!state.branch) {
      const opt = document.createElement("option");
      opt.value = ""; opt.disabled = true; opt.selected = true;
      opt.textContent = "(detached)";
      $branchSelect.appendChild(opt);
    }
    (state.branches || []).forEach(function (b) {
      const opt = document.createElement("option");
      opt.value = b; opt.selected = b === state.branch; opt.textContent = b;
      $branchSelect.appendChild(opt);
    });
    $branchSelect.disabled = state.dirty || state.rebase_in_progress;

    // Dirty / detached / rebase / stash buttons
    if (state.dirty) {
      showBanner("Working tree has uncommitted changes.", "warning");
      $btnStash.classList.remove("hidden");
    } else if (!state.branch) {
      showBanner("HEAD is detached. Select a branch from the dropdown.", "warning");
      $btnStash.classList.add("hidden");
    } else if (state.rebase_in_progress && state.conflict_files.length === 0) {
      showBanner("A rebase is in progress. Finish or abort it in your terminal to continue.", "warning");
      $btnStash.classList.add("hidden");
    } else {
      if ($banner.className === "warning") clearBanner();
      $btnStash.classList.add("hidden");
    }
    if (!state.has_stash || state.dirty) $btnStashPop.classList.add("hidden");
    else $btnStashPop.classList.remove("hidden");

    // Conflict modal
    if (state.rebase_in_progress && state.conflict_files.length > 0) {
      $conflictFiles.innerHTML = "";
      state.conflict_files.forEach(function (f) {
        const li = document.createElement("li");
        li.textContent = f;
        $conflictFiles.appendChild(li);
      });
      $conflictModal.classList.remove("hidden");
    } else {
      $conflictModal.classList.add("hidden");
    }

    const headHash = state.commits.length > 0 ? state.commits[0].commit_hash : "";
    const entries = state.branch_history || [];
    branchHistoryEntries = entries;
    headBranchHistoryIdx = entries.findIndex(function (e) { return e.commit_hash === headHash; });
    renderCommits();
    renderBranchHistory(entries, headBranchHistoryIdx);
    updateActionBar();
  }

  function showSubmoduleModal(onOk) {
    $submoduleModal.classList.remove("hidden");
    document.getElementById("btn-submodule-ok").onclick = async () => {
      $submoduleModal.classList.add("hidden");
      await onOk();
    };
    document.getElementById("btn-submodule-cancel").onclick = () => {
      $submoduleModal.classList.add("hidden");
    };
  }

  function renderCommits() {
    $commitsList.innerHTML = "";
    const mutDisabled = state.dirty || state.rebase_in_progress || !state.branch;
    state.commits.forEach(function (c, idx) {
      const row = document.createElement("div");
      row.className = "commit-row" + (selected.has(c.commit_hash) ? " selected" : "") + (c.is_head ? " is-head" : "");
      row.dataset.commitHash = c.commit_hash;
      row.dataset.idx = idx;
      row.dataset.message = c.message;

      // Drag handle
      const handle = document.createElement("span");
      handle.className = "drag-handle";
      handle.textContent = "\u2807";
      if (c.commit_hash !== INDEX_HASH) {
        handle.addEventListener("mousedown", onDragStart);
      } else {
        handle.style.visibility = "hidden";
      }
      row.appendChild(handle);

      // Cloud indicator for pushed commits
      const cloud = document.createElement("span");
      cloud.className = "pushed-indicator";
      cloud.textContent = c.pushed ? "☁" : "";
      row.appendChild(cloud);

      // Short hash
      const sh = document.createElement("span");
      sh.className = "short-hash";
      sh.textContent = c.short_hash;
      row.appendChild(sh);

      // Badges
      const badges = document.createElement("span");
      badges.className = "badges";
      c.branches.forEach(function (b) {
        const s = document.createElement("span");
        s.className = b === INDEX_HASH ? "badge-index" : "badge-branch";
        s.textContent = b === INDEX_HASH ? "Index" : b;
        badges.appendChild(s);
      });
      c.tags.forEach(function (t) {
        const s = document.createElement("span");
        s.className = "badge-tag";
        s.textContent = t;
        badges.appendChild(s);
      });
      row.appendChild(badges);

      // Message
      const msg = document.createElement("span");
      msg.className = "message";
      msg.textContent = c.message;
      row.appendChild(msg);

      // Author
      const author = document.createElement("span");
      author.className = "author";
      author.textContent = c.author;
      row.appendChild(author);

      // Date
      const dt = document.createElement("span");
      dt.className = "date";
      dt.textContent = c.date ? c.date.slice(0, 16) : "";
      row.appendChild(dt);

      // Row actions
      const actions = document.createElement("span");
      actions.className = "row-actions";

      const btnFixup = document.createElement("button");
      btnFixup.innerHTML = '<img src="/static/fixup.png" width="18" height="18" alt="Fixup">';
      btnFixup.title = "Fixup";
      btnFixup.className = "btn-fixup";
      btnFixup.disabled = mutDisabled || c.commit_hash === INDEX_HASH || idx === state.commits.length - 1;
      btnFixup.addEventListener("click", function (e) {
        e.stopPropagation();
        doRebase("fixup", {commit_hashes: [c.commit_hash]}, idx - 1);
      });
      actions.appendChild(btnFixup);

      row.appendChild(actions);

      // Click to select
      row.addEventListener("click", function (e) {
        if (e.target.closest(".row-actions") || e.target.closest(".drag-handle")) return;
        onRowClick(c.commit_hash, idx, e);
      });

      row.addEventListener("dblclick", function (e) {
        if (e.target.closest(".row-actions") || e.target.closest(".drag-handle")) return;
        e.preventDefault();
        if (c.commit_hash !== INDEX_HASH && !c.is_head) doReset(c.commit_hash);
      });

      $commitsList.appendChild(row);
    });
  }

  function renderBranchHistory(entries, headIdx) {
    $branchHistoryList.innerHTML = "";
    const headHash = state.commits.length > 0 ? state.commits[0].commit_hash : "";
    const canMutate = !state.dirty && !state.rebase_in_progress && !!state.branch;
    $btnUndo.disabled = !canMutate || headIdx === -1 || headIdx === entries.length - 1;
    $btnRedo.disabled = !canMutate || headIdx <= 0;
    entries.forEach(function (entry) {
      const isHead = entry.commit_hash === headHash;
      const row = document.createElement("div");
      row.className = "branch-history-row" + (isHead ? " is-head" : "");

      const hashSpan = document.createElement("span");
      hashSpan.className = "branch-history-hash";
      hashSpan.textContent = entry.commit_hash.slice(0, 7);
      row.appendChild(hashSpan);

      const labelSpan = document.createElement("span");
      labelSpan.className = "branch-history-label";
      labelSpan.textContent = entry.label;
      row.appendChild(labelSpan);

      const tsSpan = document.createElement("span");
      tsSpan.className = "branch-history-timestamp";
      tsSpan.textContent = entry.timestamp ? entry.timestamp.slice(0, 16) : "";
      row.appendChild(tsSpan);

      row.addEventListener("dblclick", function (e) {
        e.preventDefault();
        if (!isHead) doReset(entry.commit_hash);
      });
      $branchHistoryList.appendChild(row);
    });
  }

  // ---- Selection ----
  function setSingleSelection(hash) {
    selected = new Set([hash]);
    anchor = hash;
  }

  function applySelectionClasses() {
    document.querySelectorAll(".commit-row").forEach(function (r) {
      r.classList.toggle("selected", selected.has(r.dataset.commitHash));
    });
  }

  function onRowClick(hash, idx, e) {
    if (hash === INDEX_HASH) {
      setSingleSelection(INDEX_HASH);
    } else if (e.shiftKey && anchor !== null) {
      const anchorIdx = state.commits.findIndex(function (c) { return c.commit_hash === anchor; });
      if (anchorIdx === -1) { setSingleSelection(hash); }
      else {
        const lo = Math.min(anchorIdx, idx);
        const hi = Math.max(anchorIdx, idx);
        selected = new Set();
        for (let i = lo; i <= hi; i++) selected.add(state.commits[i].commit_hash);
      }
    } else {
      setSingleSelection(hash);
    }
    showDiff(hash);
    applySelectionClasses();
    updateActionBar();
  }

  function updateActionBar() {
    const mutDisabled = state.dirty || state.rebase_in_progress || !state.branch;
    const showReword = selected.size === 1;
    const showSquash = selected.size >= 2 && !selected.has(INDEX_HASH);

    $btnReword.classList.toggle("hidden", !showReword);
    if (showReword) $btnReword.disabled = mutDisabled;

    $btnSquash.classList.toggle("hidden", !showSquash);
    if (showSquash) $btnSquash.disabled = mutDisabled;
  }

  // ---- Reword ----
  function startReword(row, commit) {
    if (commit.commit_hash === INDEX_HASH || state.dirty || state.rebase_in_progress || busy) return;
    const msgEl = row.querySelector(".message");
    const ta = document.createElement("textarea");
    ta.className = "reword-input";
    ta.value = commit.message;
    ta.rows = Math.max(2, commit.message.split("\n").length);
    msgEl.replaceWith(ta);
    ta.focus();
    ta.select();

    function finish(save) {
      ta.removeEventListener("keydown", onKey);
      ta.removeEventListener("blur", onBlur);
      if (save && ta.value !== commit.message) {
        doRebase("reword", {commit_hashes: [commit.commit_hash], new_message: ta.value}, state.commits.findIndex(function (c) { return c.commit_hash === commit.commit_hash; }));
      } else {
        renderCommits();
      }
    }
    function onKey(e) {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) finish(true);
      if (e.key === "Escape") finish(false);
    }
    function onBlur() { if (document.hasFocus()) finish(true); }
    ta.addEventListener("keydown", onKey);
    ta.addEventListener("blur", onBlur);
  }

  // ---- Drag & drop (move) ----
  function removeDragIndicator() {
    const el = $commitsList.querySelector(".drop-indicator");
    if (el) el.remove();
  }

  function cancelDrag() {
    document.removeEventListener("mousemove", onDragMove);
    document.removeEventListener("mouseup", onDragEnd);
    delete $commitsList.dataset.dragging;
    removeDragIndicator();
    if (dragState) {
      dragState.originalRows.forEach(function (r) { $commitsList.appendChild(r); });
      document.querySelectorAll(".commit-row.dragging").forEach(function (r) { r.classList.remove("dragging"); r.style.opacity = ""; });
    }
    dragState = null;
  }

  function onDragStart(e) {
    if (state.dirty || state.rebase_in_progress || !state.branch || busy) return;
    e.preventDefault();
    const row = e.target.closest(".commit-row");
    const hash = row.dataset.commitHash;

    if (!selected.has(hash)) {
      selected = new Set([hash]);
      anchor = hash;
    }

    const originalRows = Array.from($commitsList.querySelectorAll(".commit-row"));
    dragState = {originalRows: originalRows, currentInsert: -1};

    document.querySelectorAll(".commit-row").forEach(function (r) {
      if (selected.has(r.dataset.commitHash)) { r.classList.add("dragging"); r.style.opacity = "0.4"; }
    });

    document.addEventListener("mousemove", onDragMove);
    document.addEventListener("mouseup", onDragEnd);
    $commitsList.dataset.dragging = "1";
  }

  function onDragMove(e) {
    if (!dragState) return;
    const rows = Array.from($commitsList.querySelectorAll(".commit-row"));

    let insertIdx = rows.length;
    for (let i = 0; i < rows.length; i++) {
      const rect = rows[i].getBoundingClientRect();
      if (e.clientY < rect.top + rect.height / 2) { insertIdx = i; break; }
    }

    const selectedRows = rows.filter(function (r) { return selected.has(r.dataset.commitHash); });
    const nonSelectedRows = rows.filter(function (r) { return !selected.has(r.dataset.commitHash); });

    let nonSelInsert = 0;
    for (let i = 0; i < insertIdx; i++) {
      if (!selected.has(rows[i].dataset.commitHash)) nonSelInsert++;
    }

    if (nonSelInsert === dragState.currentInsert) return;
    dragState.currentInsert = nonSelInsert;

    removeDragIndicator();
    nonSelectedRows.slice(0, nonSelInsert).forEach(function (r) { $commitsList.appendChild(r); });
    selectedRows.forEach(function (r) { $commitsList.appendChild(r); });
    nonSelectedRows.slice(nonSelInsert).forEach(function (r) { $commitsList.appendChild(r); });

    const firstDragging = $commitsList.querySelector(".commit-row.dragging");
    if (firstDragging) {
      const ind = document.createElement("div");
      ind.className = "drop-indicator";
      $commitsList.insertBefore(ind, firstDragging);
    }
  }

  function onDragEnd() {
    document.removeEventListener("mousemove", onDragMove);
    document.removeEventListener("mouseup", onDragEnd);
    delete $commitsList.dataset.dragging;
    removeDragIndicator();
    document.querySelectorAll(".commit-row.dragging").forEach(function (r) { r.classList.remove("dragging"); r.style.opacity = ""; });

    if (!dragState) return;
    const originalRows = dragState.originalRows;
    dragState = null;

    const rows = Array.from($commitsList.querySelectorAll(".commit-row"));
    const newOrder = rows.map(function (r) { return r.dataset.commitHash; });
    const originalOrder = originalRows.map(function (r) { return r.dataset.commitHash; });

    const newOrderFiltered = newOrder.filter(function (h) { return h !== INDEX_HASH; });
    const originalOrderFiltered = originalOrder.filter(function (h) { return h !== INDEX_HASH; });
    if (newOrderFiltered.join(",") === originalOrderFiltered.join(",")) return;
    doRebase("move", {order: newOrderFiltered}, selected.size === 1 ? newOrderFiltered.indexOf([...selected][0]) : null);
  }

  // ---- Diff resize ----
  $diffResize.addEventListener("mousedown", function (e) {
    e.preventDefault();
    const startY = e.clientY, startH = $diffPane.offsetHeight;
    function onMove(e) { $diffPane.style.height = Math.max(60, startH - (e.clientY - startY)) + "px"; }
    function onUp() { document.removeEventListener("mousemove", onMove); document.removeEventListener("mouseup", onUp); }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });

  // ---- Diff pane ----
  async function showDiff(hash) {
    try {
      const data = await apiGet("/api/show?commit_hash=" + hash);
      if (data.ok) {
        // Colorize added lines green and deleted lines red, skipping +++ / --- headers
        $diffContent.innerHTML = (data.diff || "").split("\n").map(function (line) {
          const escaped = line.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
          if (/^\+(?!\+\+)/.test(line)) return '<span class="diff-add">' + escaped + '</span>';
          if (/^-(?!--)/.test(line)) return '<span class="diff-del">' + escaped + '</span>';
          return escaped;
        }).join("\n");
        $diffFiles.innerHTML = "";
        (data.diff || "").split("\n").forEach(function (line) {
          const m = line.match(/^diff --git .+ b\/(.+)$/);
          if (m) { const d = document.createElement("div"); d.textContent = m[1]; $diffFiles.appendChild(d); }
        });
        $diffPane.classList.remove("hidden");
      } else {
        showBanner(data.error || "Failed to load diff", "error");
      }
    } catch (err) { showBanner("Failed to load diff: " + err.message, "error"); }
  }

  // ---- API actions ----
  async function doRebase(operation, params, selectIdx) {
    const body = Object.assign({operation: operation}, params);
    const data = await withSpinner(() => apiPost("/api/rebase", body));
    if (data) handleResponse(data, selectIdx);
  }

  async function refreshState() {
    const data = await withSpinner(() => apiGet("/api/state"));
    if (data) handleResponse(data);
  }

  function handleResponse(data, selectIdx) {
    if (data.ok || data.conflict) {
      state = data;
      const validHashes = new Set(state.commits.map(c => c.commit_hash));
      selected = new Set(Array.from(selected).filter(h => validHashes.has(h)));
      if (selectIdx != null) {
        const idx = Math.max(0, Math.min(selectIdx, state.commits.length - 1));
        setSingleSelection(state.commits[idx].commit_hash);
      } else if (selected.size === 0 && state.commits.length > 0) {
        setSingleSelection(state.commits[0].commit_hash);
      }
      clearBanner();
      render();
      if (selected.size > 0) showDiff([...selected][0]);
    } else {
      const errorMessages = {
        "gitmodules_differ": "Reset to a different set of subrepos is not supported.",
        "gitmodules_in_range": "Cannot reorder: range contains a commit that changes .gitmodules.",
      };
      showBanner(errorMessages[data.error] || data.message || data.error || "Operation failed", "error");
    }
  }

  // ---- Event wiring ----
  async function doReset(hash) {
    const data = await withSpinner(() => apiPost("/api/reset", {commit_hash: hash}));
    if (data) {
      handleResponse(data);
      if (data.ok && data.submodule_update_suggested) {
        hideSpinner();
        showSubmoduleModal(async () => {
          const subData = await withSpinner(() => apiPost("/api/submodule/update"));
          if (subData) handleResponse(subData);
        });
      }
    }
  }

  $btnUndo.addEventListener("click", () => doReset(branchHistoryEntries[headBranchHistoryIdx + 1].commit_hash));
  $btnRedo.addEventListener("click", () => doReset(branchHistoryEntries[headBranchHistoryIdx - 1].commit_hash));

  $btnRefresh.addEventListener("click", refreshState);

  $btnStash.addEventListener("click", () => withSpinner(() => apiPost("/api/stash")).then(d => d && handleResponse(d)));
  $btnStashPop.addEventListener("click", () => withSpinner(() => apiPost("/api/stash/pop")).then(d => d && handleResponse(d)));

  $btnReword.addEventListener("click", function () {
    if (selected.size !== 1) return;
    const hash = [...selected][0];
    const row = $commitsList.querySelector('[data-commit-hash="' + hash + '"]');
    const commit = state.commits.find(function (c) { return c.commit_hash === hash; });
    if (row && commit) startReword(row, commit);
  });

  $btnSquash.addEventListener("click", function () {
    if (selected.size < 2) return;
    // commits are newest-first; findIndex returns the newest selected index, which is where squash places the result.
    doRebase("squash", {commit_hashes: Array.from(selected)}, state.commits.findIndex(function (c) { return selected.has(c.commit_hash); }));
  });

  $btnQuit.addEventListener("click", function () {
    fetch("/api/quit", {method: "POST", headers: headers(), keepalive: true}).catch(() => {});
    window.close();
    document.body.innerHTML = "<p>Server stopped. Close this tab.</p>";
  });

  $btnAbort.addEventListener("click", () => withSpinner(() => apiPost("/api/rebase/abort")).then(d => d && handleResponse(d)));
  $btnContinue.addEventListener("click", () => withSpinner(() => apiPost("/api/rebase/continue")).then(d => d && handleResponse(d)));

  // Keyboard shortcuts
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      if (dragState) cancelDrag();
      if (selected.size) { selected.clear(); anchor = null; renderCommits(); }
    }
  });

  $branchSelect.addEventListener("change", async function () {
    selected.clear();
    anchor = null;
    const data = await withSpinner(() => apiPost("/api/switch", {branch: this.value}));
    if (data) {
      handleResponse(data);
      if (data.ok && data.submodule_update_suggested) {
        hideSpinner();
        showSubmoduleModal(async () => {
          const subData = await withSpinner(() => apiPost("/api/submodule/update"));
          if (subData) handleResponse(subData);
        });
      }
    }
  });

  // Help modals
  function setupHelpModal(btnId, closeId, modal) {
    document.getElementById(btnId).addEventListener("click", () => modal.classList.remove("hidden"));
    document.getElementById(closeId).addEventListener("click", () => modal.classList.add("hidden"));
  }
  setupHelpModal("commits-help-btn", "commits-help-close", $commitsHelpModal);
  setupHelpModal("branch-history-help-btn", "branch-history-help-close", $branchHistoryHelpModal);

  // Auto-refresh on window focus
  window.addEventListener("focus", refreshState);

  // ---- Initial load ----
  refreshState();
})();
