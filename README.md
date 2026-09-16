# ModSync

> **Status: Early Development / MVP**

ModSync is a small cross-platform command-line manager for shareable modpacks. Give friends a `modpack.json`; ModSync downloads each enabled mod, verifies its checksum, installs it into an isolated directory, and records enough state to detect missing or damaged files later.

## Features

- Install ZIP archives and regular files from HTTP(S) URLs.
- Stream downloads with progress instead of loading them into memory.
- Verify optional source SHA256 checksums before installation.
- Safely extract ZIPs with traversal and symbolic-link protection.
- Track versions and per-file SHA256 checksums locally.
- Repair missing or damaged mods and update only changed mods.
- Continue processing the pack when one download fails.

## Requirements

- Python 3.12 or newer
- Windows, macOS, or Linux
- Network access to the URLs listed in the modpack

## Installation

Clone or download this repository, then create an isolated environment:

```bash
cd modsync
python -m venv .venv
```

Activate it on macOS/Linux:

```bash
source .venv/bin/activate
```

Or on Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install ModSync:

```bash
python -m pip install .
```

For development and tests, use `python -m pip install ".[dev]"`. Reinstall after changing the package before testing the generated `modsync` command.

## Usage

```bash
modsync install modpack.json
modsync verify modpack.json
modsync update modpack.json
modsync info modpack.json
```

`install` and `update` are idempotent: a mod whose version and installed files already match is skipped. Missing, changed, or damaged mods are downloaded again. Disabled mods remain untouched.

## Modpack format

Paths in `install_directory` are resolved relative to the JSON file. Mod names must be unique. SHA256 is optional, but strongly recommended when publishers provide a trusted digest.

```json
{
  "name": "Karim Valheim Pack",
  "version": "1.0.0",
  "description": "Modpack for playing with friends",
  "game": "Valheim",
  "install_directory": "./mods",
  "mods": [
    {
      "name": "ExampleMod",
      "version": "1.2.0",
      "url": "https://example.com/ExampleMod.zip",
      "sha256": null,
      "enabled": true
    }
  ]
}
```

See [`examples/modpack.example.json`](examples/modpack.example.json) for a ready-to-edit copy.

## Project structure

```text
modsync/
├── modsync/       # CLI, configuration, downloader, installer, and verifier
├── tests/         # Unit tests with no live network requests
├── examples/      # Example modpack
├── pyproject.toml # Package metadata and console entry point
├── README.md
└── LICENSE
```

## Security

ModSync treats every download as untrusted data. It never executes downloaded files. ZIP entries are checked before extraction; absolute paths, parent traversal, drive-qualified paths, and symbolic links are rejected. Archive entry-count and expanded-size limits reduce common ZIP bomb risks. Installation is staged beneath the configured install directory, and TLS verification remains enabled by `requests`.

For best protection, use HTTPS URLs and fill in `sha256` from a trusted source. A checksum proves file identity, not that a mod itself is safe. Review mods and their publishers before loading them into a game.

## Development

```bash
python -m pip install ".[dev]"
python -m pytest
```

Tests cover valid and invalid configuration, hashing, version checks, safe ZIP installation, ZIP Slip rejection, missing mods, and failed downloads.

## Roadmap

- GUI
- Multiple game profiles
- Automatic game detection
- Rollback and backup
- Exporting custom modpacks
- Mod dependencies
- GitHub Releases as a download source
- Thunderstore integration
- ModSync self-update
- Friend-to-friend modpack synchronization

These are planned directions, not features of the current MVP.

## Current limitations

- Sources are direct HTTP(S) file URLs only.
- The state file must remain present to verify installed versions.
- Disabled or removed mods are not automatically deleted.
- There is no rollback, backup, dependency resolver, authentication, or GUI.
- ModSync does not determine whether a downloaded mod is trustworthy or compatible with a game.

## License

ModSync is available under the [MIT License](LICENSE).
