"""Create icon/version resources and accompanying Windows release metadata."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from commander_gui import __version__

PACKAGES = ("PySide6", "PySide6-Essentials", "PySide6-Addons", "shiboken6", "psutil", "PyInstaller",
            "chrome_version", "requests", "user_agents", "zendriver", "websockets", "mss", "deprecated",
            "wrapt", "ua-parser", "ua-parser-builtins", "certifi", "charset-normalizer", "idna", "urllib3",
            "asyncio-atexit", "grapheme", "emoji")


def prepare(destination: Path) -> None:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QImage

    destination.mkdir(parents=True, exist_ok=True)
    image = QImage(str(REPO / "icon.png"))
    if image.isNull():
        raise RuntimeError("Could not read icon.png")
    images = []
    for size in (16, 32, 48, 256):
        pixels = image.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        content = QByteArray()
        buffer = QBuffer(content)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not pixels.save(buffer, "PNG"):
            raise RuntimeError("Could not encode the Windows icon")
        images.append((size, bytes(content)))
    directory = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for size, content in images:
        dimension = 0 if size == 256 else size
        directory.extend(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(content), offset))
        offset += len(content)
    (destination / "commander.ico").write_bytes(bytes(directory) + b"".join(data for _, data in images))
    numbers = [int(n) for n in __version__.split(".")[:3]]
    version = tuple((numbers + [0, 0, 0, 0])[:4])
    (destination / "version-info.txt").write_text(f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={version!r}, prodvers={version!r}, mask=0x3f,
    flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'STALKER GAMMA Commander contributors'),
    StringStruct('FileDescription', 'STALKER GAMMA Commander'),
    StringStruct('FileVersion', {__version__!r}),
    StringStruct('ProductName', 'STALKER GAMMA Commander'),
    StringStruct('ProductVersion', {__version__!r}),
    StringStruct('OriginalFilename', 'STALKER-GAMMA-COMMANDER.exe')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
''', encoding="utf-8")


def finalise(bundle: Path, generated: Path) -> None:
    shutil.copy2(generated / "commander.ico", bundle / "commander.ico")
    shutil.copy2(REPO / "LICENSE", bundle / "LICENSE.txt")
    shutil.copy2(REPO / "packaging/windows/PORTABLE.txt", bundle / "README.txt")
    shutil.copy2(REPO / "packaging/windows/THIRD-PARTY-NOTICES.txt", bundle / "THIRD-PARTY-NOTICES.txt")
    licenses = bundle / "licenses"
    licenses.mkdir(exist_ok=True)
    for license_file in (REPO / "packaging/windows/licenses").glob("*"):
        if license_file.is_file():
            shutil.copy2(license_file, licenses / license_file.name)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
    for package in PACKAGES:
        dist = importlib.metadata.distribution(package)
        for file in dist.files or []:
            if any(word in file.name.upper() for word in ("LICENSE", "COPYING", "COPYRIGHT", "NOTICE")):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    target = licenses / package / file.name
                    target.parent.mkdir(exist_ok=True)
                    shutil.copy2(source, target)
    backend = json.loads((REPO / "cli/windows-backend.json").read_text())
    backend_build = json.loads((REPO / "cli/windows/commander-cli-build.json").read_text(encoding="utf-8-sig"))
    if backend_build["source_revision"] != backend["source_revision"]:
        raise RuntimeError("CLI build does not match the pinned source revision")
    revision = subprocess.run(
        ["git", "-c", f"safe.directory={REPO.as_posix()}", "rev-parse", "HEAD"],
        cwd=REPO, capture_output=True, text=True, check=False,
    ).stdout.strip()
    info = {
        "application_version": __version__, "source_revision": revision,
        "python": sys.version, "architecture": "windows-x64", "backend": backend,
        "backend_build": backend_build,
        "packages": {name: importlib.metadata.version(name) for name in PACKAGES},
    }
    (bundle / "build-info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    sources = bundle / "source"
    sources.mkdir(exist_ok=True)
    shutil.copy2(REPO / "cli/windows-source.zip", sources / "stalker-gamma-cli-source.zip")
    with zipfile.ZipFile(sources / "commander-source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in ("commander_gui", "assistant", "scripts", "packaging/windows", "tests", ".github"):
            for source in sorted((REPO / folder).rglob("*")):
                if source.is_file() and "__pycache__" not in source.parts and source.suffix not in (".pyc", ".pyo"):
                    archive.write(source, source.relative_to(REPO).as_posix())
        for name in ("LICENSE", "README.md", "icon.png", "requirements.txt", "requirements-dev.txt",
                     "requirements-build-windows.txt", "run.ps1", "cli/windows-backend.json", "cli/stalker-gamma.png"):
            archive.write(REPO / name, name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "finalise", "archive"))
    parser.add_argument("--generated", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.generated)
    else:
        if args.bundle is None:
            parser.error("finalise requires --bundle")
        if args.action == "finalise":
            finalise(args.bundle, args.generated)
        else:
            if args.output is None:
                parser.error("archive requires --output")
            with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(args.bundle.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(args.bundle.parent).as_posix())
