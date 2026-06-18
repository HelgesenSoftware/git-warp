#!/usr/bin/env python3
"""publish_release.py <version>  — create the GitHub release and upload to PyPI.

Run after make_release.py and the manual CHANGELOG.md review/code review.
Requires the GitHub CLI (`gh`, already authenticated) and `twine`.
PyPI credentials are read from the environment (TWINE_USERNAME/TWINE_PASSWORD,
or a configured ~/.pypirc) — this script never sees or asks for the key.
"""

import os
import re
import shutil
import subprocess
import sys


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: publish_release.py <version>")
    version = sys.argv[1]
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        sys.exit(f"version must be X.Y.Z, got '{version}'")

    tag = f"v{version}"
    subprocess.run(["git", "rev-parse", tag], check=True, capture_output=True, text=True)

    subprocess.run(["git", "push", "origin", "master", tag], check=True)

    subprocess.run(
        ["gh", "release", "create", tag, "--title", version, "--notes-file", "CHANGELOG.md", "--latest"],
        check=True,
    )
    print(f"GitHub release {tag} published.")

    os.remove("CHANGELOG.md")

    shutil.rmtree("dist", ignore_errors=True)
    subprocess.run(["python", "-m", "build"], check=True)
    subprocess.run(["twine", "upload", "dist/*"], check=True)
    print(f"Uploaded {version} to PyPI.")

    subprocess.run(["git", "switch", "develop"], check=True)
    subprocess.run(["git", "reset", "master"], check=True)
    subprocess.run(["git", "add", "CLAUDE.md"], check=True)
    subprocess.run(["git", "commit", "-m", "Add CLAUDE.md"], check=True)


if __name__ == "__main__":
    main()
