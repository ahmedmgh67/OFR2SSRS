"""Build a self-contained oracle2ssrs wheel for machines with no internet.

Every runtime dependency is unpacked into oracle2ssrs/_vendor/ inside the
wheel (and, for Windows, the ReportViewer DLLs into tools/renderlab/lib/)
and the wheel declares no dependencies, so on the target machine

    pip install --no-index oracle2ssrs-<ver>-cp313-cp313-win_amd64.whl

installs everything from that one file. lxml, PyMuPDF and MarkupSafe are
compiled, so the wheel is tied to one OS and Python version (default:
Windows x64, Python 3.13 - the documented server).

Run on a machine WITH internet, from the repo root:

    python -m pip install build wheel
    python tools/build_offline_wheel.py                      # Windows x64, 3.13
    python tools/build_offline_wheel.py --python-version 3.15
    python tools/build_offline_wheel.py --platform macosx_11_0_arm64

pip evaluates dependency markers against the running interpreter, so the
build re-checks the whole dependency tree for the target platform and
Python version and stops if anything is missing.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

from packaging.requirements import Requirement  # ships with `build`
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]

# Pulled in only on Windows (click needs it there), so pip download on
# another OS would skip it. Harmless where unused.
EXTRA = ["colorama"]


def target_env(platform: str, pyver: str) -> dict:
    """Marker environment of the target machine (not the building one)."""
    if platform.startswith("win"):
        osn, sysp, system = "nt", "win32", "Windows"
        machine = "ARM64" if "arm64" in platform else "AMD64"
    elif platform.startswith("macosx"):
        osn, sysp, system = "posix", "darwin", "Darwin"
        machine = "arm64" if platform.endswith("arm64") else "x86_64"
    else:
        osn, sysp, system = "posix", "linux", "Linux"
        machine = platform.rsplit("_", 1)[-1] if "aarch64" not in platform else "aarch64"
    return dict(os_name=osn, sys_platform=sysp, platform_system=system,
                platform_machine=machine, python_version=pyver,
                python_full_version=pyver + ".0", implementation_name="cpython",
                platform_python_implementation="CPython", platform_release="",
                platform_version="", extra="")


def check_tree(vendor: Path, roots: list[str], env: dict) -> None:
    """Fail unless every requirement reachable from `roots` - evaluated for
    the target platform - is vendored at a satisfying version."""
    have = {}
    for meta in vendor.glob("*.dist-info/METADATA"):
        msg = Parser().parsestr(meta.read_text(encoding="utf-8"))
        have[canonicalize_name(msg["Name"])] = (Version(msg["Version"]),
                                               msg.get_all("Requires-Dist") or [])
    todo = [(Requirement(r), "oracle2ssrs") for r in roots]
    seen, problems = set(), []
    while todo:
        req, parent = todo.pop()
        for extra in sorted(req.extras) or [""]:
            if req.marker and not req.marker.evaluate({**env, "extra": extra}):
                continue
            name = canonicalize_name(req.name)
            if name not in have:
                problems.append(f"missing {req} (needed by {parent})")
                continue
            version, reqs = have[name]
            if not req.specifier.contains(version, prereleases=True):
                problems.append(f"{req} but bundled {version} (needed by {parent})")
            if (name, extra) in seen:
                continue
            seen.add((name, extra))
            for r in map(Requirement, reqs):
                if r.marker and "extra" in str(r.marker):
                    if not r.marker.evaluate({**env, "extra": extra}):
                        continue
                    r.marker = None
                todo.append((r, name))
    if problems:
        sys.exit("Dependency tree incomplete for the target:\n  " + "\n  ".join(problems))
    print(f"  dependency tree complete: {len({n for n, _ in seen})} packages")


ENGINE_NOTICE = """\
Third-party components in this folder
=====================================

The DLLs here are Microsoft redistributables, Copyright (c) Microsoft
Corporation, downloaded unmodified from nuget.org by
tools/renderlab/fetch_reportviewer.py:

  Microsoft.ReportViewer.Runtime.WinForms 12.0.2402.15
  Microsoft.ReportViewer.Runtime.Common   12.0.2402.15
      (Microsoft Report Viewer 2015 Runtime)
  Microsoft.SqlServer.Types               12.0.5000.0
      (Microsoft System CLR Types for SQL Server; license in
       licenses/license_SysClrTypes.rtf)

They are licensed by Microsoft under Microsoft's terms, not under this
project's MIT license, and may run on Windows only. By installing them you
agree to those terms.
"""


def add_engine(lib: Path) -> None:
    """Download the ReportViewer DLLs into the wheel tree."""
    sys.path.insert(0, str(ROOT / "tools" / "renderlab"))
    import fetch_reportviewer as fetch  # noqa: PLC0415
    fetch.LIB = lib
    if fetch.main() != 0:
        sys.exit("Could not fetch the ReportViewer DLLs (see above).")
    (lib / "THIRD_PARTY_NOTICES.txt").write_text(ENGINE_NOTICE, encoding="utf-8")
    if not (lib / "licenses" / "license_SysClrTypes.rtf").exists():
        sys.exit("Microsoft.SqlServer.Types license file missing from the package.")


def run(*cmd: str) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--platform", default="win_amd64")
    ap.add_argument("--python-version", default="3.13")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--no-engine", action="store_true",
                    help="leave out the ReportViewer DLLs (Windows wheels "
                         "include them by default)")
    args = ap.parse_args()

    pyver = args.python_version
    if f"{sys.version_info.major}.{sys.version_info.minor}" != pyver:
        # pip download evaluates markers for this interpreter; check_tree()
        # below re-checks the whole tree for the target and fails on a gap.
        print(f"note: building for Python {pyver} from "
              f"{sys.version_info.major}.{sys.version_info.minor}")
    cp = "cp" + pyver.replace(".", "")
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        run(sys.executable, "-m", "build", "--wheel", "-o", str(tmp / "base"), str(ROOT))
        base = next((tmp / "base").glob("oracle2ssrs-*.whl"))

        run(sys.executable, "-m", "pip", "download", "--disable-pip-version-check",
            "--only-binary=:all:", "--platform", args.platform,
            "--python-version", pyver, "--implementation", "cp",
            "-d", str(tmp / "deps"), *deps, *EXTRA)

        run(sys.executable, "-m", "wheel", "unpack", "-d", str(tmp / "u"), str(base))
        tree = next((tmp / "u").iterdir())
        vendor = tree / "oracle2ssrs" / "_vendor"
        vendor.mkdir()
        for whl in sorted((tmp / "deps").glob("*.whl")):
            with zipfile.ZipFile(whl) as z:
                z.extractall(vendor)
            print(f"  vendored {whl.name}")

        check_tree(vendor, deps + EXTRA, target_env(args.platform, pyver))

        # The Preview/batch render engine is Windows-only and its license
        # allows Windows only, so only Windows wheels carry it.
        if args.platform.startswith("win") and not args.no_engine:
            add_engine(tree / "oracle2ssrs" / "tools" / "renderlab" / "lib")

        # No Requires-Dist: pip must never try to fetch anything.
        info = next(tree.glob("*.dist-info"))
        meta = info / "METADATA"
        meta.write_text("".join(
            line for line in meta.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.startswith(("Requires-Dist:", "Provides-Extra:"))
        ), encoding="utf-8")
        # Platform tag; the vendored C extensions are not pure Python.
        wheel = info / "WHEEL"
        text = wheel.read_text(encoding="utf-8")
        text = text.replace("Root-Is-Purelib: true", "Root-Is-Purelib: false")
        text = re.sub(r"^Tag: .*$", f"Tag: {cp}-{cp}-{args.platform}", text, flags=re.M)
        wheel.write_text(text, encoding="utf-8")
        (info / "RECORD").unlink()

        (tmp / "packed").mkdir()  # wheel pack does not create it
        run(sys.executable, "-m", "wheel", "pack", "-d", str(tmp / "packed"), str(tree))
        built = next((tmp / "packed").glob("*.whl"))
        dest = out / built.name
        shutil.copy2(built, dest)
    print(f"\nBuilt {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
