from modsync.hashing import sha256_file
from modsync.models import Mod, Modpack
from modsync.state import save_state
from modsync.verifier import verify_modpack


def test_detects_version_mismatch_and_corrupted_file(tmp_path):
    root = tmp_path / "mods"
    installed = root / "ExampleMod" / "mod.dll"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"corrupted")
    save_state(
        root,
        {
            "schema_version": 1,
            "modpack": {"name": "Pack", "version": "1"},
            "mods": {
                "ExampleMod": {
                    "version": "1.0",
                    "source_sha256": "a" * 64,
                    "directory": "ExampleMod",
                    "files": {"mod.dll": sha256_file(installed)},
                }
            },
        },
    )
    # Change the file after recording its installed hash.
    installed.write_bytes(b"changed again")
    mod = Mod(name="ExampleMod", version="2.0", url="https://example.com/mod.zip")
    pack = Modpack("Pack", "2", "", "Game", root, (mod,), tmp_path / "modpack.json")

    report = verify_modpack(pack)

    messages = [issue.message for issue in report.issues]
    assert any("version mismatch" in message for message in messages)
    assert any("checksum mismatch" in message for message in messages)


def test_reports_missing_mod(tmp_path):
    mod = Mod(name="MissingMod", version="1", url="https://example.com/mod.zip")
    pack = Modpack("Pack", "1", "", "Game", tmp_path / "mods", (mod,), tmp_path / "pack.json")

    report = verify_modpack(pack)

    assert not report.ok
    assert "not recorded" in report.issues[0].message
