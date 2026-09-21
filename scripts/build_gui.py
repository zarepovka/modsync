"""Build and archive one native ModSync desktop package on the current OS."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import PyInstaller.__main__
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
ARTIFACTS = ROOT / "artifacts"


def render_packaging_icon(destination: Path) -> Path:
    """Render the canonical SVG so PyInstaller can create native ICO/ICNS data."""
    renderer = QSvgRenderer(str(ROOT / "modsync" / "gui" / "resources" / "modsync.svg"))
    if not renderer.isValid():
        raise RuntimeError("ModSync SVG icon is invalid")
    image = QImage(512, 512, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    if not image.save(str(destination), "PNG"):
        raise RuntimeError("Could not render ModSync packaging icon")
    return destination


def build() -> tuple[Path, Path]:
    with tempfile.TemporaryDirectory(prefix="modsync-icon-") as temporary:
        icon = render_packaging_icon(Path(temporary) / "modsync.png")
        bundle_mode = "--onedir" if sys.platform == "darwin" else "--onefile"
        PyInstaller.__main__.run(
            [
                str(ROOT / "scripts" / "modsync_gui_entry.py"),
                "--noconfirm",
                "--clean",
                bundle_mode,
                "--windowed",
                "--name=ModSync",
                "--collect-data=modsync.gui.resources",
                f"--icon={icon}",
                f"--paths={ROOT}",
            ]
        )
    if sys.platform == "darwin":
        bundle = DIST / "ModSync.app"
        executable = bundle / "Contents" / "MacOS" / "ModSync"
    elif os.name == "nt":
        bundle = DIST / "ModSync.exe"
        executable = bundle
    else:
        bundle = DIST / "ModSync"
        executable = bundle
    if not executable.is_file():
        raise RuntimeError(f"PyInstaller output is missing: {executable}")
    return bundle, executable


def smoke_test(executable: Path) -> None:
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    subprocess.run(
        [str(executable), "--smoke-test"],
        check=True,
        timeout=60,
        env=environment,
    )


def archive(bundle: Path) -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        destination = ARTIFACTS / "ModSync-macOS.zip"
        base = destination.with_suffix("")
        shutil.make_archive(str(base), "zip", root_dir=DIST, base_dir=bundle.name)
        return destination
    if os.name == "nt":
        destination = ARTIFACTS / "ModSync-Windows-x64.zip"
        base = destination.with_suffix("")
        shutil.make_archive(str(base), "zip", root_dir=DIST, base_dir=bundle.name)
        return destination
    destination = ARTIFACTS / "ModSync-Linux-x64.tar.gz"
    with tarfile.open(destination, "w:gz") as output:
        output.add(bundle, arcname="ModSync")
    return destination


if __name__ == "__main__":
    built_bundle, built_executable = build()
    smoke_test(built_executable)
    result = archive(built_bundle)
    print(result)
