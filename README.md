# git warp

Rewrite git history in your browser — drag to reorder, squash, fixup, reword, and split commits, with unlimited undo.

A command-line tool, run from inside any git repository. It starts a local HTTP server bound to `127.0.0.1`, opens a browser, and presents a single-page UI for rewriting git history. All git operations run server-side via `subprocess`; the browser is pure UI. It refuses mutating operations on a dirty working tree; commits are shown with short hashes.

Design goal: minimal, reliable code with no avoidable failure modes. Dependencies: Flask, stdlib. No build step or JS bundler.

## Install

```bash
pip install git-warp
```

This installs the `git-warp` program, which git automatically exposes as the `git warp` subcommand.

## Usage

Run from inside any git repository:

```bash
git warp
```

This opens your browser to a local page where you can view and rewrite your commit history. From there, open the manual for details.

## Requirements

- Python >= 3.10
- Git >= 2.26

## License

GPL-3.0 — see [LICENSE.md](LICENSE.md).
