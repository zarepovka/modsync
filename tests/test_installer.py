import shutil
import zipfile
from pathlib import Path

from modsync.hashing import sha256_file
from modsync.installer import Installer
from modsync.models import Mod, Modpack


class CopyDownloader:
    def __init__(self, source: Path):
        self.source = source

    def download(self, mod, destination, progress=None):
        target = destination / self.source.name
        shutil.copy2(self.source, target)
        return target


def make_pack(tmp_path, mod):
    return Modpack(
        name="Test Pack",
        version="1.0",
        description="",
        game="Test Game",
        install_directory=tmp_path / "mods",
        mods=(mod,),
        source_path=tmp_path / "modpack.json",
    )


def test_installs_zip_and_skips_verified_mod(tmp_path):
    archive = tmp_path / "example.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("plugins/example.dll", b"safe binary data")
        bundle.writestr("README.txt", b"hello")
    mod = Mod(
        name="ExampleMod",
        version="1.2.0",
        url="https://example.com/example.zip",
        sha256=sha256_file(archive),
    )
    installer = Installer(downloader=CopyDownloader(archive))
    pack = make_pack(tmp_path, mod)

    first = installer.install_modpack(pack)
    second = installer.install_modpack(pack)

    assert first.installed == 1
    assert not first.failures
    assert (pack.install_directory / "ExampleMod" / "plugins" / "example.dll").is_file()
    assert second.skipped == 1


def test_rejects_zip_slip(tmp_path):
    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../../escaped.txt", b"nope")
    mod = Mod(name="BadMod", version="1", url="https://example.com/bad.zip")
    pack = make_pack(tmp_path, mod)

    report = Installer(downloader=CopyDownloader(archive)).install_modpack(pack)

    assert report.installed == 0
    assert len(report.failures) == 1
    assert "Unsafe path" in report.failures[0].message
    assert not (tmp_path / "escaped.txt").exists()


def test_update_replaces_only_changed_mod(tmp_path):
    first_archive = tmp_path / "version-one.zip"
    with zipfile.ZipFile(first_archive, "w") as bundle:
        bundle.writestr("plugin.txt", b"version one")
    mod_a_v1 = Mod(
        name="ModA",
        version="1",
        url="https://example.com/mod-a.zip",
        sha256=sha256_file(first_archive),
    )
    mod_b = Mod(
        name="ModB",
        version="1",
        url="https://example.com/mod-b.zip",
        sha256=sha256_file(first_archive),
    )
    initial_pack = Modpack(
        "Pack",
        "1",
        "",
        "Game",
        tmp_path / "mods",
        (mod_a_v1, mod_b),
        tmp_path / "pack.json",
    )
    Installer(downloader=CopyDownloader(first_archive)).install_modpack(initial_pack)
    mod_b_file = initial_pack.install_directory / "ModB" / "plugin.txt"
    untouched_before = mod_b_file.stat().st_mtime_ns

    second_archive = tmp_path / "version-two.zip"
    with zipfile.ZipFile(second_archive, "w") as bundle:
        bundle.writestr("plugin.txt", b"version two")
    mod_a_v2 = Mod(
        name="ModA",
        version="2",
        url="https://example.com/mod-a.zip",
        sha256=sha256_file(second_archive),
    )
    updated_pack = Modpack(
        "Pack",
        "2",
        "",
        "Game",
        initial_pack.install_directory,
        (mod_a_v2, mod_b),
        initial_pack.source_path,
    )

    updater = Installer(downloader=CopyDownloader(second_archive))
    report = updater.install_modpack(updated_pack)

    assert report.installed == 1
    assert report.skipped == 1
    assert (updated_pack.install_directory / "ModA" / "plugin.txt").read_bytes() == b"version two"
    assert mod_b_file.read_bytes() == b"version one"
    assert mod_b_file.stat().st_mtime_ns == untouched_before
