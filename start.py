"""Start the Mutual Fund Portfolio Analyser: one command from a fresh clone.

    python start.py                 # set up what is missing, refresh prices, open it
    python start.py --no-browser    # the same, without opening a browser tab

Standard library only, because it runs before anything is installed:

1. checks for Python 3.11 or later;
2. makes `.venv` and installs the pinned dependencies into it -- again only
   when `requirements.lock` or `pyproject.toml` has changed since;
3. runs `jobs.setup`, which on the first run loads market data from its public
   sources (a few minutes, then about 70 for NSE's index levels) and on later
   runs refreshes today's prices;
4. runs `jobs.serve`, the portal on http://127.0.0.1:8765, which opens itself in
   your browser.

Everything stays on this machine: the data under `data/`, your portfolio in an
encrypted ledger once you import a CAS statement. DECISIONS V1-72.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
#: What the installed dependencies were resolved from, so a changed lock file
#: reinstalls and an unchanged one costs nothing.
STAMP = VENV / "installed-from.sha256"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def pinned() -> str:
    digest = hashlib.sha256()
    for name in ("requirements.lock", "pyproject.toml"):
        digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()


def main() -> int:
    # Not redundant with pyproject's `requires-python`: this runs under whatever
    # Python the user typed, before pip ever reads that.
    if sys.version_info < (3, 11):  # noqa: UP036
        print(f"Python 3.11 or later is needed; this is {sys.version.split()[0]}.")
        return 1
    if not venv_python().exists():
        print("Creating a private Python environment in .venv ...")
        venv.create(VENV, with_pip=True)
    if not STAMP.exists() or STAMP.read_text(encoding="utf-8") != pinned():
        print("Installing the pinned dependencies (first time: a minute or two) ...")
        subprocess.check_call(
            [str(venv_python()), "-m", "pip", "install", "--quiet",
             "--disable-pip-version-check", "-e", ".[cas]", "-c", "requirements.lock"],
            cwd=ROOT,
        )
        STAMP.write_text(pinned(), encoding="utf-8")

    python = str(venv_python())
    # A setup step that fails is reported by `jobs.setup` and retried on the
    # next start; the portal still opens on whatever loaded.
    subprocess.call([python, "-m", "jobs.setup"], cwd=ROOT)
    return subprocess.call([python, "-m", "jobs.serve", *sys.argv[1:]], cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
