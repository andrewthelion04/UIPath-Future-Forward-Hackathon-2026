#!/usr/bin/env python3
"""Comenzi simple, identice pe Windows, macOS și Linux.

    python manage.py setup     # creează .venv și instalează dependențele (o singură dată)
    python manage.py seed      # (re)creează lumea simulată
    python manage.py api       # pornește API-ul FastAPI pe http://127.0.0.1:8000
    python manage.py ui        # pornește simulatorul Streamlit pe http://localhost:8501
    python manage.py test      # rulează testele (argumentele în plus ajung la pytest)

Pe macOS/Linux folosește `python3` dacă `python` nu există. Scriptul folosește doar
biblioteca standard și rulează restul comenzilor cu Python-ul din `.venv`, deci
mediul virtual nu trebuie activat manual.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
MIN_PYTHON = (3, 11)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(*args: str) -> int:
    python = venv_python()
    if not python.exists():
        print("No .venv found. Run first:  python manage.py setup")
        return 1
    env = dict(os.environ, PYTHONUTF8="1")
    try:
        return subprocess.call([str(python), *args], cwd=ROOT, env=env)
    except KeyboardInterrupt:
        return 130


def cmd_setup(_: list[str]) -> int:
    if sys.version_info < MIN_PYTHON:
        found = ".".join(map(str, sys.version_info[:3]))
        print(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required, found {found}.")
        return 1
    if not venv_python().exists():
        print(f"Creating virtual environment in {VENV} ...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    code = run("-m", "pip", "install", "--upgrade", "pip")
    code = code or run("-m", "pip", "install", "-r", "requirements-dev.txt")
    if code:
        return code
    env_file = ROOT / ".env"
    if not env_file.exists():
        shutil.copyfile(ROOT / ".env.example", env_file)
        print("Created .env from .env.example (local data only; no secrets or network needed).")
    print("\nSetup done. Next:  python manage.py seed  |  api  |  ui  |  test")
    return 0


def cmd_seed(extra: list[str]) -> int:
    return run("-m", "app.seeding", *extra)


def cmd_api(extra: list[str]) -> int:
    return run("-m", "app.main", *extra)


def cmd_ui(extra: list[str]) -> int:
    return run("-m", "streamlit", "run", "ui/app.py", *extra)


def cmd_test(extra: list[str]) -> int:
    return run("-m", "pytest", *extra)


COMMANDS = {"setup": cmd_setup, "seed": cmd_seed, "api": cmd_api, "ui": cmd_ui, "test": cmd_test}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return 1
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
