"""Where fetch_reportviewer.py puts each file of the NuGet packages."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "renderlab"))

import fetch_reportviewer as fetch  # noqa: E402


def test_sqlserver_types_is_pinned_to_the_v12_line():
    versions = dict(fetch.CANDIDATES)
    assert versions["Microsoft.SqlServer.Types"].startswith("12.")


def test_12x_package_layout(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch, "LIB", tmp_path)
    d = fetch._dest
    assert d("lib/net40/Microsoft.SqlServer.Types.dll") == tmp_path / "Microsoft.SqlServer.Types.dll"
    assert d("nativeBinaries/x64/SqlServerSpatial120.dll") == tmp_path / "x64" / "SqlServerSpatial120.dll"
    assert d("nativeBinaries/x86/msvcr100.dll") == tmp_path / "x86" / "msvcr100.dll"
    assert d("license/license_SysClrTypes.rtf") == tmp_path / "licenses" / "license_SysClrTypes.rtf"
    assert d("lib/Microsoft.ReportViewer.Common.dll") == tmp_path / "Microsoft.ReportViewer.Common.dll"
    for skipped in ("content/SqlServerTypes/Loader.cs", "tools/install.ps1",
                    "Microsoft.SqlServer.Types.nuspec", "nativeBinaries/x64/",
                    "lib/net472/de/Microsoft.SqlServer.Types.resources.dll"):
        assert d(skipped) is None, skipped
