# Oracle2SSRS — Downloads for Windows Server

## 1. Already on Windows Server by default

| Software | Notes |
|----------|-------|
| Windows Server 2019 / 2022 / 2025, x64 | |
| [.NET Framework 4.8](https://dotnet.microsoft.com/download/dotnet-framework/net48) | Built in. Used by the Preview tab only. |
| Windows PowerShell 5.1 | Built in. Used by the Preview tab only. |

## 2. Python

| Software | Version | Download |
|----------|---------|----------|
| Python for Windows, 64-bit installer | 3.13.16 | [python-3.13.16-amd64.exe](https://www.python.org/ftp/python/3.13.16/python-3.13.16-amd64.exe) ([all versions](https://www.python.org/downloads/windows/)) |

## 3. Python libraries

The `.whl` files are on each project's **Download files** page. For the
libraries marked *Windows build*, pick the file ending in
`cp313-win_amd64.whl` (or `abi3-win_amd64.whl` for PyMuPDF).

**Needed to run the app:**

| Library | Version | Download |
|---------|---------|----------|
| Flask | 3.1.3 | <https://pypi.org/project/Flask/3.1.3/#files> |
| Werkzeug | 3.1.9 | <https://pypi.org/project/Werkzeug/3.1.9/#files> |
| Jinja2 | 3.1.6 | <https://pypi.org/project/Jinja2/3.1.6/#files> |
| MarkupSafe *(Windows build)* | 3.0.4 | <https://pypi.org/project/MarkupSafe/3.0.4/#files> |
| itsdangerous | 2.2.0 | <https://pypi.org/project/itsdangerous/2.2.0/#files> |
| click | 8.5.0 | <https://pypi.org/project/click/8.5.0/#files> |
| blinker | 1.9.0 | <https://pypi.org/project/blinker/1.9.0/#files> |
| colorama | 0.4.6 | <https://pypi.org/project/colorama/0.4.6/#files> |
| lxml *(Windows build)* | 6.1.3 | <https://pypi.org/project/lxml/6.1.3/#files> |
| python-docx | 1.2.0 | <https://pypi.org/project/python-docx/1.2.0/#files> |
| typing_extensions | 4.16.0 | <https://pypi.org/project/typing-extensions/4.16.0/#files> |
| python-dotenv | 1.2.4 | <https://pypi.org/project/python-dotenv/1.2.4/#files> |
| pypdf | 6.21.1 | <https://pypi.org/project/pypdf/6.21.1/#files> |
| PyMuPDF *(Windows build)* | 1.28.2 | <https://pypi.org/project/PyMuPDF/1.28.2/#files> |
| waitress (web server) | 3.0.2 | <https://pypi.org/project/waitress/3.0.2/#files> |


**To Install:** on a machine that has Python, this one command downloads all of the above into a `wheels` folder:

```powershell
python -m pip download -r requirements.txt waitress colorama --platform win_amd64 --python-version 3.13 --only-binary=:all: -d wheels
```

## 4. Report preview engine

Official Microsoft packages from NuGet. Each `.nupkg` is a zip file.

| Package | Version | Download |
|---------|---------|----------|
| Microsoft.ReportViewer.Runtime.WinForms | 12.0.2402.15 | [nuget.org page](https://www.nuget.org/packages/Microsoft.ReportViewer.Runtime.WinForms/12.0.2402.15) · [direct .nupkg](https://www.nuget.org/api/v2/package/Microsoft.ReportViewer.Runtime.WinForms/12.0.2402.15) |
| Microsoft.ReportViewer.Runtime.Common | 12.0.2402.15 | [nuget.org page](https://www.nuget.org/packages/Microsoft.ReportViewer.Runtime.Common/12.0.2402.15) · [direct .nupkg](https://www.nuget.org/api/v2/package/Microsoft.ReportViewer.Runtime.Common/12.0.2402.15) |
| Microsoft.SqlServer.Types | 12.0.5000.0 | [nuget.org page](https://www.nuget.org/packages/Microsoft.SqlServer.Types/12.0.5000.0) · [direct .nupkg](https://www.nuget.org/api/v2/package/Microsoft.SqlServer.Types/12.0.5000.0) |

**To install:** from the project folder, run
`python tools/renderlab/fetch_reportviewer.py`. It downloads the three
packages and puts the DLLs in `tools\renderlab\lib\`. Copy that folder to
the server.

**Upgrading an existing server:** replace `tools\renderlab\lib\` with a
freshly fetched one at the same time as you update the app.
`RenderLab.exe.config` expects Microsoft.SqlServer.Types 12.0.0.0; a `lib\`
fetched by an earlier version (holding the 17.x DLL) makes renders fail
with "manifest definition does not match". The Windows wheels on PyPI
(1.0.4 and later) already include the right DLLs.

## 5. The app itself

The Oracle2SSRS source: <https://github.com/ahmedmgh67/OFR2SSRS> →
**Code → Download ZIP**.
