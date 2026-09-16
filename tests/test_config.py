import json

import pytest

from modsync.config import load_modpack
from modsync.exceptions import ConfigError


def test_load_valid_modpack(tmp_path):
    path = tmp_path / "modpack.json"
    path.write_text(
        json.dumps(
            {
                "name": "Friends Pack",
                "version": "1.0.0",
                "description": "For Friday nights",
                "game": "Valheim",
                "install_directory": "./mods",
                "mods": [
                    {
                        "name": "Example Mod",
                        "version": "2.1",
                        "url": "https://example.com/mod.zip",
                        "sha256": None,
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    pack = load_modpack(path)

    assert pack.name == "Friends Pack"
    assert pack.install_directory == (tmp_path / "mods").resolve()
    assert pack.mods[0].version == "2.1"


def test_invalid_json_has_readable_error(tmp_path):
    path = tmp_path / "modpack.json"
    path.write_text('{"name":', encoding="utf-8")

    with pytest.raises(ConfigError, match=r"line 1, column"):
        load_modpack(path)


def test_rejects_invalid_checksum(tmp_path):
    path = tmp_path / "modpack.json"
    path.write_text(
        json.dumps(
            {
                "name": "Pack",
                "version": "1",
                "game": "Game",
                "install_directory": "mods",
                "mods": [
                    {
                        "name": "Mod",
                        "version": "1",
                        "url": "https://example.com/mod.zip",
                        "sha256": "not-a-hash",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="sha256"):
        load_modpack(path)
