"""Oracle Reports -> SSRS converter, packaged for pip.

The app itself lives in backend/ (imported as top-level `app`, `cli` and
`converter` modules, the same way `python backend/app.py` runs it). This
package only locates that tree and starts it.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# Installed wheel: backend/ sits inside this package. Source checkout
# (or an editable install): it sits next to it.
ROOT = _HERE if (_HERE / "backend").is_dir() else _HERE.parent
BACKEND = ROOT / "backend"

# The offline wheel (tools/build_offline_wheel.py) carries its dependencies
# here; put them first so the bundled versions are the ones used.
_VENDOR = _HERE / "_vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))


def _use_backend() -> None:
    path = str(BACKEND)
    if path not in sys.path:
        sys.path.insert(0, path)
