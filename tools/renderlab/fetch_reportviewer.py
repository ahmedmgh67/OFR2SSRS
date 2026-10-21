"""Fetch the Microsoft ReportViewer 2015 runtime (RDL-2008 capable) from
nuget.org and unpack its DLLs into tools/renderlab/lib/ (gitignored).

The DLLs are Microsoft's redistributables; a .nupkg is a zip. We try a
list of known package ids so a renamed/unlisted package doesn't strand us.
License files shipped in a package land in lib/licenses/.
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path
from urllib.request import urlopen, Request

LIB = Path(__file__).resolve().parent / "lib"

# (package_id, version) candidates, in preference order. v12 = ReportViewer
# 2015 — processes RDL 2008/01 schema (v15/150.x dropped 2008 for 2016).
CANDIDATES = [
    ("Microsoft.ReportViewer.Runtime.WinForms", "12.0.2402.15"),
    ("Microsoft.ReportViewer.Runtime.Common", "12.0.2402.15"),
    # Spatial types assembly (v12.0.0.0, exactly what ReportViewer 12 binds
    # to) — the processing engine references it unconditionally even for
    # reports with no spatial data. Stay on 12.x: the 170.x package ships
    # under an evaluation-only pre-release license.
    ("Microsoft.SqlServer.Types", "12.0.5000.0"),
]

EXTRA_FALLBACKS = [
    ("Microsoft.ReportViewer.2015.Runtime", "12.0.2402.10"),
]


def _fetch(pkg: str, ver: str) -> bytes | None:
    url = f"https://www.nuget.org/api/v2/package/{pkg}/{ver}"
    try:
        req = Request(url, headers={"User-Agent": "renderlab/1.0"})
        with urlopen(req, timeout=120) as r:
            data = r.read()
        print(f"  ok  {pkg} {ver}  ({len(data)//1024} KB)")
        return data
    except Exception as e:  # noqa: BLE001
        print(f"  --  {pkg} {ver}: {type(e).__name__}: {e}")
        return None


def _dest(name: str) -> Path | None:
    """Where a file from a .nupkg goes in lib/, or None to skip it.

    Managed DLLs land flat in lib/. Satellite resource DLLs and netstandard
    builds are skipped (the engine is .NET Framework). Native DLLs
    (nativeBinaries/<arch>/ or runtimes/win-<arch>/native/) go to
    lib/<arch>/; the engine only loads them for map/spatial data, which the
    converter never emits. license/* goes to lib/licenses/.
    """
    parts = name.split("/")
    base = parts[-1]
    if not base:
        return None
    if parts[0].lower() == "license":
        return LIB / "licenses" / base
    if not base.lower().endswith(".dll"):
        return None
    if base.lower().endswith(".resources.dll"):
        return None
    if len(parts) >= 2 and parts[1].lower().startswith("netstandard"):
        return None
    if parts[0].lower() == "runtimes":
        if len(parts) != 4 or parts[2].lower() != "native":
            return None
        return LIB / parts[1].lower().removeprefix("win-") / base
    if parts[0].lower() == "nativebinaries" and len(parts) == 3:
        return LIB / parts[1].lower() / base
    return LIB / base


def _unpack_dlls(nupkg: bytes, written: set[Path]) -> int:
    n = 0
    with zipfile.ZipFile(io.BytesIO(nupkg)) as z:
        for name in z.namelist():
            dest = _dest(name)
            if dest is None:
                continue
            # First writer this run wins; a file left by an earlier run
            # (e.g. an older package version) is replaced.
            if dest in written:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(name))
            written.add(dest)
            n += 1
            print(f"      -> {dest.relative_to(LIB)}")
    return n


def main() -> int:
    LIB.mkdir(parents=True, exist_ok=True)
    got_any = False
    written: set[Path] = set()
    print("Fetching ReportViewer runtime packages from nuget.org ...")
    for pkg, ver in CANDIDATES:
        data = _fetch(pkg, ver)
        if data:
            _unpack_dlls(data, written)
            got_any = True
    needed = {"Microsoft.ReportViewer.WinForms.dll",
              "Microsoft.ReportViewer.Common.dll",
              "Microsoft.ReportViewer.ProcessingObjectModel.dll"}
    have = {p.name for p in LIB.glob("*.dll")}
    missing = needed - have
    if missing:
        print(f"Missing after primary fetch: {sorted(missing)} — trying fallbacks")
        for pkg, ver in EXTRA_FALLBACKS:
            data = _fetch(pkg, ver)
            if data:
                _unpack_dlls(data, written)
        have = {p.name for p in LIB.glob("*.dll")}
        missing = needed - have
    print(f"\nDLLs present: {sorted(have)}")
    if missing:
        print(f"STILL MISSING: {sorted(missing)}")
        return 1
    print("RenderLab ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
