"""Build and archive one native ModSync desktop package on the current OS."""

from __future__ import annotations

import os
import plistlib
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

from modsync import __version__

ROOT = Path(__file__).resolve().parents[1]
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


def build(output_root: Path) -> tuple[Path, Path]:
    dist = output_root / "dist"
    work = output_root / "build"
    spec = output_root / "spec"
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
                f"--distpath={dist}",
                f"--workpath={work}",
                f"--specpath={spec}",
            ]
        )
    if sys.platform == "darwin":
        bundle = finalize_macos_bundle(dist / "ModSync.app")
        executable = bundle / "Contents" / "MacOS" / "ModSync"
    elif os.name == "nt":
        bundle = dist / "ModSync.exe"
        executable = bundle
    else:
        bundle = dist / "ModSync"
        executable = bundle
    if not executable.is_file():
        raise RuntimeError(f"PyInstaller output is missing: {executable}")
    return bundle, executable


def finalize_macos_bundle(bundle: Path) -> Path:
    """Set release metadata and refresh the bundle's ad-hoc integrity signature."""
    clean_bundle = bundle.parent.parent / "package" / bundle.name
    clean_bundle.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ditto", "--noextattr", "--norsrc", str(bundle), str(clean_bundle)],
        check=True,
    )
    bundle = clean_bundle
    info_path = bundle / "Contents" / "Info.plist"
    with info_path.open("rb") as source:
        info = plistlib.load(source)
    info["CFBundleIdentifier"] = "io.github.zarepovka.modsync"
    info["CFBundleShortVersionString"] = __version__
    info["CFBundleVersion"] = __version__
    with info_path.open("wb") as destination:
        plistlib.dump(info, destination)
    # Build-host Finder metadata is not part of the application and prevents
    # macOS from sealing the bundle consistently.
    subprocess.run(["xattr", "-cr", str(bundle)], check=True)
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(bundle)],
        check=True,
    )
    verify_macos_bundle(bundle)
    return bundle


def verify_macos_bundle(bundle: Path) -> None:
    subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)],
        check=True,
    )


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
        destination.unlink(missing_ok=True)
        subprocess.run(
            [
                "ditto",
                "-c",
                "-k",
                "--sequesterRsrc",
                "--keepParent",
                str(bundle),
                str(destination),
            ],
            check=True,
        )
        return destination
    if os.name == "nt":
        destination = ARTIFACTS / "ModSync-Windows-x64.zip"
        base = destination.with_suffix("")
        shutil.make_archive(
            str(base), "zip", root_dir=bundle.parent, base_dir=bundle.name
        )
        return destination
    destination = ARTIFACTS / "ModSync-Linux-x64.tar.gz"
    with tarfile.open(destination, "w:gz") as output:
        output.add(bundle, arcname="ModSync")
    return destination


def verify_archive(package: Path) -> None:
    """Test the distributable itself, not only its pre-archive build tree."""
    if sys.platform != "darwin":
        return
    with tempfile.TemporaryDirectory(prefix="modsync-archive-check-") as temporary:
        subprocess.run(
            ["ditto", "-x", "-k", str(package), temporary],
            check=True,
        )
        bundle = Path(temporary) / "ModSync.app"
        verify_macos_bundle(bundle)
        smoke_test(bundle / "Contents" / "MacOS" / "ModSync")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="modsync-native-build-") as temporary:
        built_bundle, built_executable = build(Path(temporary))
        smoke_test(built_executable)
        result = archive(built_bundle)
        verify_archive(result)
        print(result)
