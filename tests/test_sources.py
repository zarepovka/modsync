import json
import shutil
import zipfile
from pathlib import Path

import pytest
import requests

from modsync.cli import main
from modsync.backup import BackupManager
from modsync.config import load_modpack
from modsync.downloader import Downloader
from modsync.exceptions import (
    InstallError,
    RollbackError,
    ConfigError,
    DownloadError,
    SourceAmbiguousError,
    SourceError,
    SourceNotFoundError,
    SourceRateLimitError,
)
from modsync.hashing import sha256_file
from modsync.installer import Installer
from modsync.models import Mod, Modpack, ResolvedMod, SourceSpec
from modsync.sources.base import SourceRegistry
from modsync.sources.direct import DirectSource
from modsync.sources.github import GitHubReleaseSource, normalize_tag, validate_repository
from modsync.state import load_state_file


class FakeResponse:
    def __init__(self, payload=None, status=200, *, url="https://api.github.com/result", content=b""):
        self.payload = payload
        self.status_code = status
        self.url = url
        self.headers = {"Content-Length": str(len(content))}
        self.content = content

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        pass


class FakeSession:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def release_payload(*, tag="v1.4.2", assets=None, draft=False, prerelease=False):
    return {
        "id": 42,
        "tag_name": tag,
        "published_at": "2026-09-16T12:00:00Z",
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets
        if assets is not None
        else [
            {
                "id": 99,
                "name": "ExampleMod.zip",
                "browser_download_url": "https://github.com/owner/repo/releases/download/v1.4.2/ExampleMod.zip",
            }
        ],
    }


def github_mod(*, release="latest", asset="ExampleMod.zip", sha256=None):
    return Mod(
        "ExampleMod",
        None,
        None,
        sha256,
        True,
        SourceSpec(
            "github",
            {"repository": "owner/repo", "release": release, "asset": asset},
        ),
    )


def resolve(payload=None, *, mod=None, token=None, session=None, sleeper=lambda delay: None):
    active_session = session or FakeSession(
        FakeResponse(release_payload() if payload is None else payload)
    )
    provider = GitHubReleaseSource(active_session, token=token, sleeper=sleeper)
    return provider.resolve(mod or github_mod()), active_session


def write_pack(path: Path, mod: dict) -> Path:
    path.write_text(
        json.dumps(
            {
                "name": "Pack",
                "version": "1",
                "game": "Game",
                "install_directory": "mods",
                "mods": [mod],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_legacy_direct_config_is_unchanged(tmp_path):
    pack = load_modpack(
        write_pack(
            tmp_path / "pack.json",
            {"name": "A", "version": "1", "url": "https://example.com/a.zip"},
        )
    )
    assert pack.mods[0].source == SourceSpec(
        "direct", {"url": "https://example.com/a.zip", "version": "1"}
    )


def test_new_direct_source_format(tmp_path):
    pack = load_modpack(
        write_pack(
            tmp_path / "pack.json",
            {"name": "A", "source": {"type": "direct", "url": "https://example.com/a.zip"}},
        )
    )
    resolved = DirectSource().resolve(pack.mods[0])
    assert resolved.version == "unversioned"
    assert resolved.download_url == "https://example.com/a.zip"


def test_direct_source_can_inherit_top_level_version(tmp_path):
    pack = load_modpack(
        write_pack(
            tmp_path / "pack.json",
            {
                "name": "A",
                "version": "2",
                "source": {"type": "direct", "url": "https://example.com/a.zip"},
            },
        )
    )
    assert DirectSource().resolve(pack.mods[0]).version == "2"


def test_github_latest_uses_official_endpoint():
    result, session = resolve()
    assert session.calls[0][0] == "https://api.github.com/repos/owner/repo/releases/latest"
    assert result.version == "1.4.2"


def test_github_tag_is_url_encoded():
    _, session = resolve(mod=github_mod(release="release/one"))
    assert session.calls[0][0].endswith("/releases/tags/release%2Fone")


def test_exact_asset_match():
    result, _ = resolve()
    assert result.filename == "ExampleMod.zip"


def test_glob_asset_match():
    result, _ = resolve(mod=github_mod(asset="Example*.zip"))
    assert result.filename == "ExampleMod.zip"


def test_asset_not_found():
    with pytest.raises(SourceNotFoundError, match="No GitHub release asset"):
        resolve(mod=github_mod(asset="missing.zip"))


def test_ambiguous_assets():
    payload = release_payload(
        assets=[
            {"id": 1, "name": "a.zip", "browser_download_url": "https://github.com/a/a"},
            {"id": 2, "name": "b.zip", "browser_download_url": "https://github.com/a/b"},
        ]
    )
    with pytest.raises(SourceAmbiguousError, match="ambiguous"):
        resolve(payload, mod=github_mod(asset="*.zip"))


@pytest.mark.parametrize("field", ["draft", "prerelease"])
def test_rejects_unstable_release(field):
    payload = release_payload(**{field: True})
    with pytest.raises(SourceError, match="not a published stable release"):
        resolve(payload)


@pytest.mark.parametrize("payload", [{}, [], {"id": 1, "tag_name": "v1"}])
def test_rejects_malformed_api_response(payload):
    with pytest.raises(SourceError, match="malformed"):
        resolve(payload)


@pytest.mark.parametrize(
    "repository",
    ["owner", "owner/repo/extra", "../repo", "owner/..", "owner/repo?x=1", "owner\n/repo"],
)
def test_repository_validation_rejects_injection(repository):
    with pytest.raises(SourceError):
        validate_repository(repository)


def test_repository_validation_accepts_normal_name():
    assert validate_repository("owner-name/repo.name") == "owner-name/repo.name"


def test_network_failure_is_readable_and_retried():
    session = FakeSession(
        requests.ConnectionError("offline"),
        requests.ConnectionError("offline"),
        requests.ConnectionError("offline"),
    )
    delays = []
    with pytest.raises(SourceError, match="Could not resolve"):
        resolve(session=session, sleeper=delays.append)
    assert delays == [1.0, 2.0]


def test_timeout_is_wrapped():
    session = FakeSession(requests.Timeout("slow"))
    provider = GitHubReleaseSource(session, token=None, sleeper=lambda delay: None, attempts=1)
    with pytest.raises(SourceError, match="Could not resolve"):
        provider.resolve(github_mod())


def test_release_404_is_not_found_without_retry():
    session = FakeSession(FakeResponse({}, 404))
    with pytest.raises(SourceNotFoundError, match="release not found"):
        resolve(session=session)
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [403, 429])
def test_rate_limit_has_friendly_error(status):
    session = FakeSession(FakeResponse({}, status))
    with pytest.raises(SourceRateLimitError, match="MODSYNC_GITHUB_TOKEN"):
        resolve(session=session)


def test_5xx_has_bounded_retry():
    session = FakeSession(FakeResponse({}, 503), FakeResponse(release_payload()))
    delays = []
    result, _ = resolve(session=session, sleeper=delays.append)
    assert result.version == "1.4.2"
    assert delays == [1.0]


def test_optional_token_is_sent_only_in_header():
    result, session = resolve(token="secret-value")
    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer secret-value"
    assert "secret-value" not in repr(result)
    assert "secret-value" not in json.dumps(result.source_metadata)


def test_no_token_means_no_authorization_header():
    _, session = resolve(token=None)
    assert "Authorization" not in session.calls[0][1]["headers"]


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("v2.4.1", "2.4.1"), ("V1.0.0-beta.1", "1.0.0-beta.1"), ("version-two", "version-two")],
)
def test_tag_normalization(tag, expected):
    assert normalize_tag(tag) == expected


@pytest.mark.parametrize("name", ["../evil.zip", r"dir\evil.zip", "bad?.zip", "bad\x00.zip"])
def test_unsafe_asset_filename(name):
    payload = release_payload(
        assets=[{"id": 1, "name": name, "browser_download_url": "https://github.com/a/b"}]
    )
    with pytest.raises(SourceError, match="filename|unsafe"):
        resolve(payload, mod=github_mod(asset=name))


def test_rejects_non_github_asset_url():
    payload = release_payload(
        assets=[{"id": 1, "name": "ExampleMod.zip", "browser_download_url": "https://evil.test/a"}]
    )
    with pytest.raises(SourceError, match="unsafe download URL"):
        resolve(payload)


def test_config_rejects_embedded_token(tmp_path):
    with pytest.raises(ConfigError, match="unsupported fields"):
        load_modpack(
            write_pack(
                tmp_path / "pack.json",
                {
                    "name": "A",
                    "source": {
                        "type": "github",
                        "repository": "owner/repo",
                        "release": "latest",
                        "asset": "a.zip",
                        "token": "nope",
                    },
                },
            )
        )


def test_registry_dispatches_both_builtin_shapes():
    registry = SourceRegistry()
    registry.register("direct", DirectSource())
    resolved = registry.resolve(Mod("A", "1", "https://example.com/a.zip"))
    assert resolved.source_metadata["type"] == "direct"


class StaticSource:
    def __init__(self, resolved):
        self.resolved = resolved

    def resolve(self, mod):
        return self.resolved


class CopyDownloader:
    def __init__(self, source):
        self.source = source

    def download(self, resolved, destination, progress=None):
        target = destination / resolved.filename
        shutil.copy2(self.source, target)
        return target


class ApplyThenFailInstaller(Installer):
    def apply_prepared_mod(self, root, prepared, displaced_root):
        super().apply_prepared_mod(root, prepared, displaced_root)
        raise InstallError("injected GitHub apply failure")


class RestoreFailingBackupManager(BackupManager):
    def restore(self, backup_id):
        raise RollbackError("injected GitHub rollback failure")


def github_resolved(archive: Path, *, tag="v1.0.0", asset_id=1, checksum=None):
    return ResolvedMod(
        name="ExampleMod",
        version=normalize_tag(tag),
        download_url=f"https://github.com/o/r/releases/download/{tag}/mod.zip",
        filename="mod.zip",
        sha256=checksum,
        source_metadata={"type": "github", "repository": "o/r", "release": "latest"},
        release_metadata={
            "tag": tag,
            "asset": "mod.zip",
            "asset_id": asset_id,
            "download_url": "https://github.com/o/r/mod.zip",
            "resolved_at": "2026-09-16T12:00:00Z",
        },
        source_identity={
            "type": "github",
            "repository": "o/r",
            "tag": tag,
            "asset": "mod.zip",
            "asset_id": asset_id,
        },
    )


def integration_pack(tmp_path, mod):
    return Modpack("Pack", "1", "", "Game", tmp_path / "mods", (mod,), tmp_path / "pack.json")


def registry_for(resolved):
    registry = SourceRegistry()
    registry.register("github", StaticSource(resolved))
    return registry


def test_github_install_stores_source_state_and_actual_sha(tmp_path):
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("plugin.txt", b"one")
    resolved = github_resolved(archive)
    pack = integration_pack(tmp_path, github_mod())
    report = Installer(CopyDownloader(archive), source_registry=registry_for(resolved)).install_modpack(pack)
    record = load_state_file(pack.install_directory / ".modsync-state.json")["mods"]["ExampleMod"]
    assert not report.failures
    assert record["source"]["identity"]["tag"] == "v1.0.0"
    assert record["source"]["sha256"] == sha256_file(archive)


def test_configured_checksum_success_and_mismatch(tmp_path):
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("plugin.txt", b"one")
    good = github_resolved(archive, checksum=sha256_file(archive))
    bad = github_resolved(archive, checksum="0" * 64)
    assert not Installer(CopyDownloader(archive), source_registry=registry_for(good)).install_modpack(
        integration_pack(tmp_path / "good", github_mod())
    ).failures
    report = Installer(CopyDownloader(archive), source_registry=registry_for(bad)).install_modpack(
        integration_pack(tmp_path / "bad", github_mod())
    )
    assert "SHA256 mismatch" in report.failures[0].message


def test_update_skips_unchanged_github_release(tmp_path):
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("plugin.txt", b"one")
    resolved = github_resolved(archive)
    pack = integration_pack(tmp_path, github_mod())
    installer = Installer(CopyDownloader(archive), source_registry=registry_for(resolved))
    installer.install_modpack(pack)
    report = installer.update_modpack(pack)
    assert report.skipped == 1
    assert report.backup_id is None


def test_update_changed_github_release_creates_backup(tmp_path):
    first = tmp_path / "one.zip"
    second = tmp_path / "two.zip"
    with zipfile.ZipFile(first, "w") as bundle:
        bundle.writestr("plugin.txt", b"one")
    with zipfile.ZipFile(second, "w") as bundle:
        bundle.writestr("plugin.txt", b"two")
    pack = integration_pack(tmp_path, github_mod())
    Installer(CopyDownloader(first), source_registry=registry_for(github_resolved(first))).install_modpack(pack)
    report = Installer(
        CopyDownloader(second),
        source_registry=registry_for(github_resolved(second, tag="v2.0.0", asset_id=2)),
    ).update_modpack(pack)
    assert report.backup_id is not None
    assert (pack.install_directory / "ExampleMod" / "plugin.txt").read_bytes() == b"two"


def test_failed_github_update_rolls_back_previous_files_and_state(tmp_path):
    first = tmp_path / "one.zip"
    second = tmp_path / "two.zip"
    with zipfile.ZipFile(first, "w") as bundle:
        bundle.writestr("plugin.txt", b"one")
    with zipfile.ZipFile(second, "w") as bundle:
        bundle.writestr("plugin.txt", b"two")
    pack = integration_pack(tmp_path, github_mod())
    Installer(CopyDownloader(first), source_registry=registry_for(github_resolved(first))).install_modpack(pack)
    before = load_state_file(pack.install_directory / ".modsync-state.json")

    report = ApplyThenFailInstaller(
        CopyDownloader(second),
        source_registry=registry_for(github_resolved(second, tag="v2.0.0", asset_id=2)),
    ).update_modpack(pack)

    assert report.rollback_succeeded is True
    assert (pack.install_directory / "ExampleMod" / "plugin.txt").read_bytes() == b"one"
    assert load_state_file(pack.install_directory / ".modsync-state.json") == before


def test_failed_github_rollback_preserves_backup_id(tmp_path):
    first = tmp_path / "one.zip"
    second = tmp_path / "two.zip"
    with zipfile.ZipFile(first, "w") as bundle:
        bundle.writestr("plugin.txt", b"one")
    with zipfile.ZipFile(second, "w") as bundle:
        bundle.writestr("plugin.txt", b"two")
    pack = integration_pack(tmp_path, github_mod())
    Installer(CopyDownloader(first), source_registry=registry_for(github_resolved(first))).install_modpack(pack)

    report = ApplyThenFailInstaller(
        CopyDownloader(second),
        source_registry=registry_for(github_resolved(second, tag="v2.0.0", asset_id=2)),
        backup_manager_factory=RestoreFailingBackupManager,
    ).update_modpack(pack)

    assert report.rollback_succeeded is False
    assert report.backup_id is not None
    assert "injected GitHub rollback failure" in (report.rollback_error or "")


def test_state_round_trip_preserves_release_metadata(tmp_path):
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("plugin.txt", b"one")
    resolved = github_resolved(archive)
    pack = integration_pack(tmp_path, github_mod())
    Installer(CopyDownloader(archive), source_registry=registry_for(resolved)).install_modpack(pack)
    record = load_state_file(pack.install_directory / ".modsync-state.json")["mods"]["ExampleMod"]
    assert record["source"]["release"]["resolved_at"].endswith("Z")


def test_info_shows_github_source_and_resolved_version(tmp_path, capsys):
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("plugin.txt", b"one")
    config = write_pack(
        tmp_path / "pack.json",
        {
            "name": "ExampleMod",
            "source": {
                "type": "github",
                "repository": "owner/repo",
                "release": "latest",
                "asset": "ExampleMod.zip",
            },
        },
    )
    pack = load_modpack(config)
    resolved = github_resolved(archive, tag="v3.2.1")
    Installer(CopyDownloader(archive), source_registry=registry_for(resolved)).install_modpack(pack)

    assert main(["info", str(config)]) == 0

    output = capsys.readouterr().out
    assert "3.2.1" in output
    assert "source: github" in output


def test_downloader_rejects_https_to_http_redirect(tmp_path):
    response = FakeResponse(content=b"data", url="http://github.com/insecure")
    downloader = Downloader(FakeSession(response))
    resolved = github_resolved(tmp_path / "unused")
    with pytest.raises(DownloadError, match="Unsafe redirect"):
        downloader.download(resolved, tmp_path / "downloads")


def test_downloader_accepts_github_cdn_redirect(tmp_path):
    response = FakeResponse(
        content=b"data", url="https://release-assets.githubusercontent.com/example/mod.zip"
    )
    downloader = Downloader(FakeSession(response))
    resolved = github_resolved(tmp_path / "unused")

    downloaded = downloader.download(resolved, tmp_path / "downloads")

    assert downloaded.read_bytes() == b"data"
