# Native Windows onedir bundle. Build through scripts/Build-Windows.ps1.
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_data_files, copy_metadata

repo = Path(SPECPATH).resolve().parents[1]
generated = repo / "build" / "windows" / "metadata"

datas = [
    (str(repo / "commander_gui" / "assets"), "commander_gui/assets"),
    (str(repo / "commander_gui" / "fonts" / "Exo2-Variable.ttf"), "commander_gui/fonts"),
    (str(repo / "assistant" / "fonts" / "Exo2-Variable.ttf"), "assistant/fonts"),
    (str(repo / "cli" / "stalker-gamma.png"), "cli"),
    (str(repo / "cli" / "windows-backend.json"), "cli"),
    (str(repo / "commander_gui" / "_vendor" / "CF-Clearance-Scraper-LICENSE.txt"), "commander_gui/_vendor"),
]
# Enumerate the backend explicitly so direct PyInstaller builds also omit the
# upstream experimental Python server, even if an older cache still contains it.
backend = repo / "cli" / "windows"
datas += [
    (str(path), str(Path("cli/windows") / path.relative_to(backend).parent))
    for path in sorted(backend.rglob("*"))
    if path.is_file() and path.name.lower() != "cloudscraper.exe"
]
datas += collect_data_files("ua_parser")
datas += collect_data_files("grapheme") + collect_data_files("emoji")
for package in ("PySide6", "PySide6-Essentials", "PySide6-Addons", "shiboken6", "psutil"):
    datas += copy_metadata(package)

analysis = Analysis(
    [str(repo / "packaging" / "windows" / "entrypoint.py")],
    pathex=[str(repo)],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("commander_gui.locales") + collect_submodules("zendriver"),
    excludes=["pytest", "ruff", "tkinter", "Steamdeck"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [],
    exclude_binaries=True,
    name="STALKER-GAMMA-COMMANDER",
    console=False,
    debug=False,
    strip=False,
    upx=False,
    icon=str(generated / "commander.ico"),
    version=str(generated / "version-info.txt"),
    manifest=str(repo / "packaging" / "windows" / "commander.manifest"),
)
bundle = COLLECT(
    exe, analysis.binaries, analysis.datas,
    strip=False, upx=False, name="STALKER-GAMMA-COMMANDER",
)
