"""Compatibility package for running tests with `PYTHONPATH=.`.

The real application package lives under `src/app/`. Extending `__path__`
lets imports like `app.main` resolve whether the backend is executed via an
editable install, `PYTHONPATH=src`, or the repo-root verification command from
the project docs (`PYTHONPATH=.`).
"""

from pathlib import Path

__path__.append(str(Path(__file__).resolve().parent.parent / "src" / "app"))
