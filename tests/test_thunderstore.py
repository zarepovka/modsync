import json
import shutil
import zipfile
from pathlib import Path

import pytest
import requests

from modsync.cli import main
from modsync.config import load_modpack
from modsync.exceptions import (
    ConfigError,
    DependencyConflictError,
    DependencyCycleError,
    InstallError,
    SourceError,
    SourceNotFoundError,
)
from modsync.installer import Installer
from modsync.models import Mod, Modpack, ResolvedMod, SourceSpec
from modsync.profiles import ProfileStore
from modsync.sources.base import SourceRegistry
from modsync.sources.direct import DirectSource
from modsync.sources.thunderstore import (
    API_ROOT,
    ThunderstoreSource,
    parse_dependency,
)
from modsync.state import load_state_file


class FakeResponse:
    def __init__(self, payload=None, status=200, *, url=None):
        self.payload = payload
        self.status_code = status
        self.url = url
        self.headers = {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        pass


class RoutingSession:
    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url not in self.routes:
            raise AssertionError(f"Unexpected HTTP request: {url}")
        configured = self.routes[url]
        if isinstance(configured, list):
            result = configured.pop(0)
        else:
            result = configured
        if isinstance(result, Exception):
            raise result
        if result.url is None:
            result.url = url
        return result


def package_url(namespace="Author", package="Package"):
    return f"{API_ROOT}/{namespace}/{package}/"


def version_url(namespace="Author", package="Package", version="1.0.0"):
    return f"{API_ROOT}/{namespace}/{package}/{version}/"


def version_payload(
    namespace="Author",
    package="Package",
    version="1.0.0",
    dependencies=None,
    *,
    download_url=None,
    active=True,
):
    return {
        "namespace": namespace,
        "name": package,
        "version_number": version,
        "dependencies": dependencies or [],
        "download_url": download_url
        or f"https://thunderstore.io/package/download/{namespace}/{package}/{version}/",
        "date_created": "2026-09-19T10:00:00Z",
        "is_active": active,
    }


def package_payload(
    namespace="Author",
    package="Package",
    version="1.0.0",
    dependencies=None,
    *,
    community="valheim",
    deprecated=False,
):
    return {
        "namespace": namespace,
        "name": package,
        "is_deprecated": deprecated,
        "latest": version_payload(namespace, package, version, dependencies),
        "community_listings": [{"community": community}],
    }


def thunder_mod(
    *,
    name="ExampleMod",
    community="valheim",
    namespace="Author",
    package="Package",
    version="latest",
):
    return Mod(
        name,
        None if version == "latest" else version,
        None,
        source=SourceSpec(
            "thunderstore",
            {
                "community": community,
                "namespace": namespace,
                "package": package,
                "version": version,
            },
        ),
    )


def provider_for(routes, *, attempts=1, sleeper=lambda delay: None):
    session = RoutingSession(routes)
    return ThunderstoreSource(
        session=session, attempts=attempts, sleeper=sleeper
    ), session


def resolve_latest(payload=None):
    provider, session = provider_for(
        {package_url(): FakeResponse(payload or package_payload())}
    )
    return provider.resolve(thunder_mod()), session


def make_pack(tmp_path, mods, *, name="Pack"):
    return Modpack(
        name,
        "1.0.0",
        "",
        "Game",
        tmp_path / "mods",
        tuple(mods),
        tmp_path / "modpack.json",
    )


def make_archive(path, package="Package", version="1.0.0", dependencies=None, **extra):
    manifest = {
        "name": package,
        "version_number": version,
        "dependencies": dependencies or [],
    }
    manifest.update(extra)
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
        bundle.writestr("plugins/mod.dll", f"{package}-{version}".encode())
    return path


class ArchiveDownloader:
    def __init__(self, archives):
        self.archives = archives
        self.calls = []

    def download(self, resolved, destination, progress=None):
        source_type = resolved.source_metadata["type"]
        if source_type == "thunderstore":
            key = (resolved.source_metadata["package"], resolved.version)
        else:
            key = resolved.name
        self.calls.append(key)
        source = self.archives[key]
        if isinstance(source, Exception):
            raise source
        target = destination / resolved.filename
        shutil.copy2(source, target)
        return target


class StaticSource:
    def __init__(self, resolved):
        self.resolved = resolved

    def resolve(self, mod):
        return self.resolved


class FailOnPackageInstaller(Installer):
    def __init__(self, package, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.package = package

    def apply_prepared_mod(self, root, prepared, displaced_root):
        super().apply_prepared_mod(root, prepared, displaced_root)
        if prepared.resolved.source_metadata.get("package") == self.package:
            raise InstallError("injected dependency installation failure")


def registry_with(provider, *, direct=False, github=None):
    registry = SourceRegistry()
    registry.register("thunderstore", provider)
    if direct:
        registry.register("direct", DirectSource())
    if github is not None:
        registry.register("github", StaticSource(github))
    return registry


def test_resolves_latest_version():
    resolved, session = resolve_latest()
    assert resolved.version == "1.0.0"
    assert resolved.source_metadata["requested_version"] == "latest"
    assert session.calls[0][0] == package_url()


def test_resolves_specific_version_without_substituting_latest():
    routes = {
        package_url(): FakeResponse(package_payload(version="2.0.0")),
        version_url(version="1.4.2"): FakeResponse(
            version_payload(version="1.4.2")
        ),
    }
    provider, _ = provider_for(routes)
    resolved = provider.resolve(thunder_mod(version="1.4.2"))
    assert resolved.version == "1.4.2"
    assert resolved.source_metadata["requested_version"] == "1.4.2"


def test_package_not_found():
    provider, _ = provider_for({package_url(): FakeResponse({}, 404)})
    with pytest.raises(SourceNotFoundError, match="package Author/Package"):
        provider.resolve(thunder_mod())


def test_specific_version_not_found():
    provider, _ = provider_for(
        {
            package_url(): FakeResponse(package_payload()),
            version_url(version="1.4.2"): FakeResponse({}, 404),
        }
    )
    with pytest.raises(SourceNotFoundError, match="version 1.4.2 was not found"):
        provider.resolve(thunder_mod(version="1.4.2"))


@pytest.mark.parametrize("payload", [[], {}, {"namespace": "Other"}])
def test_malformed_response(payload):
    provider, _ = provider_for({package_url(): FakeResponse(payload)})
    with pytest.raises(SourceError, match="malformed|mismatched"):
        provider.resolve(thunder_mod())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("community", "../valheim"),
        ("community", "https://evil.test"),
        ("namespace", "Bad/Author"),
        ("namespace", "Bad\\Author"),
        ("package", "../Package"),
        ("package", "Bad\x00Package"),
    ],
)
def test_invalid_source_identifiers_are_rejected_by_config(tmp_path, field, value):
    source = {
        "type": "thunderstore",
        "community": "valheim",
        "namespace": "Author",
        "package": "Package",
        "version": "latest",
    }
    source[field] = value
    path = tmp_path / "pack.json"
    path.write_text(
        json.dumps(
            {
                "name": "Pack",
                "version": "1",
                "game": "Game",
                "install_directory": "mods",
                "mods": [{"name": "Mod", "source": source}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_modpack(path)


def test_unknown_community_has_readable_error():
    payload = package_payload()
    payload["community_listings"] = [{"community": "other-game"}]
    provider, _ = provider_for({package_url(): FakeResponse(payload)})
    with pytest.raises(SourceNotFoundError, match="community 'valheim'"):
        provider.resolve(thunder_mod())


def test_download_metadata_has_safe_canonical_filename():
    resolved, _ = resolve_latest()
    assert resolved.filename == "Author-Package-1.0.0.zip"
    assert resolved.download_url.startswith("https://thunderstore.io/package/download/")


def test_manifest_is_validated_during_install(tmp_path):
    archive = make_archive(tmp_path / "package.zip")
    provider, _ = provider_for({package_url(): FakeResponse(package_payload())})
    pack = make_pack(tmp_path, [thunder_mod()])
    report = Installer(
        ArchiveDownloader({("Package", "1.0.0"): archive}),
        source_registry=registry_with(provider),
    ).install_modpack(pack)
    assert not report.failures
    assert report.installed == 1
    state = load_state_file(pack.state_path or pack.install_directory / ".modsync-state.json")
    source = state["mods"]["ExampleMod"]["source"]
    assert source["type"] == "thunderstore"
    assert source["community"] == "valheim"
    assert source["namespace"] == "Author"
    assert source["package"] == "Package"
    assert source["version"] == "1.0.0"
    assert source["download_url"].startswith("https://thunderstore.io/")
    assert source["resolved_at"].endswith("Z")
    assert len(source["sha256"]) == 64


@pytest.mark.parametrize(
    ("manifest_name", "manifest_version", "message"),
    [
        ("WrongPackage", "1.0.0", "name mismatch"),
        ("Package", "2.0.0", "version mismatch"),
    ],
)
def test_manifest_api_mismatch_stops_before_install(
    tmp_path, manifest_name, manifest_version, message
):
    archive = make_archive(
        tmp_path / "package.zip", package=manifest_name, version=manifest_version
    )
    provider, _ = provider_for({package_url(): FakeResponse(package_payload())})
    pack = make_pack(tmp_path, [thunder_mod()])
    report = Installer(
        ArchiveDownloader({("Package", "1.0.0"): archive}),
        source_registry=registry_with(provider),
    ).install_modpack(pack)
    assert message in report.failures[0].message
    assert not (pack.install_directory / "ExampleMod").exists()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Author-Library-1.2.3", ("Author", "Library", "1.2.3")),
        ("Legacy-Author-Library-1.2.3", ("Legacy-Author", "Library", "1.2.3")),
    ],
)
def test_dependency_parsing(value, expected):
    assert parse_dependency(value) == expected


@pytest.mark.parametrize(
    "value", ["Library-1.0.0", "Author-Library-latest", "../A-Lib-1.0.0", "A-B-1.0"]
)
def test_malicious_or_invalid_dependency_string(value):
    with pytest.raises(SourceError):
        parse_dependency(value)


def recursive_routes():
    return {
        package_url(): FakeResponse(
            package_payload(dependencies=["Deps-Library-1.0.0"])
        ),
        package_url("Deps", "Library"): FakeResponse(
            package_payload("Deps", "Library", dependencies=["Deps-Helper-2.0.0"])
        ),
        version_url("Deps", "Library", "1.0.0"): FakeResponse(
            version_payload(
                "Deps", "Library", "1.0.0", ["Deps-Helper-2.0.0"]
            )
        ),
        package_url("Deps", "Helper"): FakeResponse(
            package_payload("Deps", "Helper", version="2.0.0")
        ),
        version_url("Deps", "Helper", "2.0.0"): FakeResponse(
            version_payload("Deps", "Helper", "2.0.0")
        ),
    }


def test_recursive_dependencies_are_resolved():
    provider, _ = provider_for(recursive_routes())
    resolved = provider.resolve(thunder_mod())
    assert resolved.dependencies[0].source_metadata["package"] == "Library"
    assert resolved.dependencies[0].dependencies[0].source_metadata["package"] == "Helper"


def test_dependency_plan_is_deduplicated_and_dependency_first():
    routes = recursive_routes()
    root = package_payload(
        dependencies=["Deps-Library-1.0.0", "Deps-Other-1.0.0"]
    )
    routes[package_url()] = FakeResponse(root)
    routes[package_url("Deps", "Other")] = FakeResponse(
        package_payload(
            "Deps", "Other", dependencies=["Deps-Helper-2.0.0"]
        )
    )
    routes[version_url("Deps", "Other", "1.0.0")] = FakeResponse(
        version_payload("Deps", "Other", "1.0.0", ["Deps-Helper-2.0.0"])
    )
    provider, _ = provider_for(routes)
    registry = registry_with(provider)
    plan = registry.resolve_plan((thunder_mod(),))
    packages = [item.resolved.source_metadata["package"] for item in plan]
    assert packages.count("Helper") == 1
    assert packages[-1] == "Package"
    assert packages.index("Helper") < packages.index("Library")


def test_dependency_conflict_is_reported():
    routes = {
        package_url(): FakeResponse(
            package_payload(
                dependencies=["Deps-Library-1.0.0", "Deps-Other-1.0.0"]
            )
        ),
        package_url("Deps", "Library"): FakeResponse(
            package_payload("Deps", "Library")
        ),
        version_url("Deps", "Library", "1.0.0"): FakeResponse(
            version_payload("Deps", "Library", "1.0.0")
        ),
        package_url("Deps", "Other"): FakeResponse(
            package_payload(
                "Deps", "Other", dependencies=["Deps-Library-2.0.0"]
            )
        ),
        version_url("Deps", "Other", "1.0.0"): FakeResponse(
            version_payload("Deps", "Other", "1.0.0", ["Deps-Library-2.0.0"])
        ),
        version_url("Deps", "Library", "2.0.0"): FakeResponse(
            version_payload("Deps", "Library", "2.0.0")
        ),
    }
    provider, _ = provider_for(routes)
    with pytest.raises(DependencyConflictError, match="both 1.0.0 and 2.0.0"):
        provider.resolve(thunder_mod())


def test_circular_dependency_reports_chain():
    routes = {
        package_url(): FakeResponse(
            package_payload(dependencies=["Deps-Library-1.0.0"])
        ),
        package_url("Deps", "Library"): FakeResponse(
            package_payload(
                "Deps", "Library", dependencies=["Author-Package-1.0.0"]
            )
        ),
        version_url("Deps", "Library", "1.0.0"): FakeResponse(
            version_payload(
                "Deps", "Library", "1.0.0", ["Author-Package-1.0.0"]
            )
        ),
        version_url("Author", "Package", "1.0.0"): FakeResponse(
            version_payload(
                "Author", "Package", "1.0.0", ["Deps-Library-1.0.0"]
            )
        ),
    }
    provider, _ = provider_for(routes)
    with pytest.raises(DependencyCycleError, match="Package.*Library.*Package"):
        provider.resolve(thunder_mod())


def test_dependencies_download_before_dependent(tmp_path):
    root_archive = make_archive(
        tmp_path / "root.zip", dependencies=["Deps-Library-1.0.0"]
    )
    dep_archive = make_archive(
        tmp_path / "dep.zip", package="Library", version="1.0.0"
    )
    routes = {
        package_url(): FakeResponse(
            package_payload(dependencies=["Deps-Library-1.0.0"])
        ),
        package_url("Deps", "Library"): FakeResponse(
            package_payload("Deps", "Library")
        ),
        version_url("Deps", "Library", "1.0.0"): FakeResponse(
            version_payload("Deps", "Library", "1.0.0")
        ),
    }
    provider, _ = provider_for(routes)
    downloader = ArchiveDownloader(
        {("Package", "1.0.0"): root_archive, ("Library", "1.0.0"): dep_archive}
    )
    report = Installer(
        downloader, source_registry=registry_with(provider)
    ).install_modpack(make_pack(tmp_path, [thunder_mod()]))
    assert not report.failures
    assert downloader.calls == [("Library", "1.0.0"), ("Package", "1.0.0")]


def test_pinned_package_does_not_move_to_latest(tmp_path):
    archive = make_archive(tmp_path / "pinned.zip", version="1.4.2")
    routes = {
        package_url(): FakeResponse(package_payload(version="2.0.0")),
        version_url(version="1.4.2"): FakeResponse(
            version_payload(version="1.4.2")
        ),
    }
    provider, _ = provider_for(routes)
    pack = make_pack(tmp_path, [thunder_mod(version="1.4.2")])
    installer = Installer(
        ArchiveDownloader({("Package", "1.4.2"): archive}),
        source_registry=registry_with(provider),
    )
    assert not installer.install_modpack(pack).failures
    assert installer.update_modpack(pack).skipped == 1


def test_latest_update_and_no_update_paths(tmp_path):
    first = make_archive(tmp_path / "one.zip", version="1.0.0")
    second = make_archive(tmp_path / "two.zip", version="2.0.0")
    pack = make_pack(tmp_path, [thunder_mod()])
    provider1, _ = provider_for(
        {package_url(): FakeResponse(package_payload(version="1.0.0"))}
    )
    initial = Installer(
        ArchiveDownloader({("Package", "1.0.0"): first}),
        source_registry=registry_with(provider1),
    )
    assert not initial.install_modpack(pack).failures
    assert initial.update_modpack(pack).skipped == 1

    provider2, _ = provider_for(
        {package_url(): FakeResponse(package_payload(version="2.0.0"))}
    )
    report = Installer(
        ArchiveDownloader({("Package", "2.0.0"): second}),
        source_registry=registry_with(provider2),
    ).update_modpack(pack)
    assert not report.failures
    assert report.backup_id is not None
    state = load_state_file(pack.install_directory / ".modsync-state.json")
    assert state["mods"]["ExampleMod"]["version"] == "2.0.0"


def test_dependency_update_replaces_same_package_version(tmp_path):
    root1 = make_archive(
        tmp_path / "root1.zip", version="1.0.0", dependencies=["Deps-Lib-1.0.0"]
    )
    lib1 = make_archive(tmp_path / "lib1.zip", package="Lib", version="1.0.0")
    root2 = make_archive(
        tmp_path / "root2.zip", version="2.0.0", dependencies=["Deps-Lib-2.0.0"]
    )
    lib2 = make_archive(tmp_path / "lib2.zip", package="Lib", version="2.0.0")

    def routes(version):
        dependency = f"Deps-Lib-{version}"
        return {
            package_url(): FakeResponse(
                package_payload(version=version, dependencies=[dependency])
            ),
            package_url("Deps", "Lib"): FakeResponse(
                package_payload("Deps", "Lib", version=version)
            ),
            version_url("Deps", "Lib", version): FakeResponse(
                version_payload("Deps", "Lib", version)
            ),
        }

    pack = make_pack(tmp_path, [thunder_mod()])
    provider1, _ = provider_for(routes("1.0.0"))
    Installer(
        ArchiveDownloader(
            {("Package", "1.0.0"): root1, ("Lib", "1.0.0"): lib1}
        ),
        source_registry=registry_with(provider1),
    ).install_modpack(pack)
    provider2, _ = provider_for(routes("2.0.0"))
    report = Installer(
        ArchiveDownloader(
            {("Package", "2.0.0"): root2, ("Lib", "2.0.0"): lib2}
        ),
        source_registry=registry_with(provider2),
    ).update_modpack(pack)
    assert not report.failures
    assert (pack.install_directory / "Deps-Lib").is_dir()
    assert load_state_file(pack.install_directory / ".modsync-state.json")["mods"][
        "Deps-Lib"
    ]["version"] == "2.0.0"


def test_removed_dependency_is_left_as_an_orphan(tmp_path):
    root1 = make_archive(
        tmp_path / "root1.zip", dependencies=["Deps-OldLibrary-1.0.0"]
    )
    old_dependency = make_archive(
        tmp_path / "old.zip", package="OldLibrary", version="1.0.0"
    )
    root2 = make_archive(
        tmp_path / "root2.zip", version="2.0.0", dependencies=["Deps-NewLibrary-1.0.0"]
    )
    new_dependency = make_archive(
        tmp_path / "new.zip", package="NewLibrary", version="1.0.0"
    )
    first_routes = {
        package_url(): FakeResponse(
            package_payload(dependencies=["Deps-OldLibrary-1.0.0"])
        ),
        package_url("Deps", "OldLibrary"): FakeResponse(
            package_payload("Deps", "OldLibrary")
        ),
        version_url("Deps", "OldLibrary"): FakeResponse(
            version_payload("Deps", "OldLibrary")
        ),
    }
    second_routes = {
        package_url(): FakeResponse(
            package_payload(
                version="2.0.0", dependencies=["Deps-NewLibrary-1.0.0"]
            )
        ),
        package_url("Deps", "NewLibrary"): FakeResponse(
            package_payload("Deps", "NewLibrary")
        ),
        version_url("Deps", "NewLibrary"): FakeResponse(
            version_payload("Deps", "NewLibrary")
        ),
    }
    pack = make_pack(tmp_path, [thunder_mod()])
    provider1, _ = provider_for(first_routes)
    first_report = Installer(
        ArchiveDownloader(
            {
                ("Package", "1.0.0"): root1,
                ("OldLibrary", "1.0.0"): old_dependency,
            }
        ),
        source_registry=registry_with(provider1),
    ).install_modpack(pack)
    assert not first_report.failures

    provider2, _ = provider_for(second_routes)
    second_report = Installer(
        ArchiveDownloader(
            {
                ("Package", "2.0.0"): root2,
                ("NewLibrary", "1.0.0"): new_dependency,
            }
        ),
        source_registry=registry_with(provider2),
    ).update_modpack(pack)
    assert not second_report.failures
    state = load_state_file(pack.install_directory / ".modsync-state.json")
    assert "Deps-OldLibrary" in state["mods"]
    assert "Deps-NewLibrary" in state["mods"]
    assert (pack.install_directory / "Deps-OldLibrary").is_dir()


def test_deprecated_latest_produces_warning_but_pinned_does_not():
    latest, _ = resolve_latest(package_payload(deprecated=True))
    assert "deprecated" in latest.warnings[0]
    routes = {
        package_url(): FakeResponse(package_payload(deprecated=True)),
        version_url(): FakeResponse(version_payload()),
    }
    provider, _ = provider_for(routes)
    assert provider.resolve(thunder_mod(version="1.0.0")).warnings == ()


@pytest.mark.parametrize("error", [requests.Timeout("slow"), requests.ConnectionError("offline")])
def test_network_errors_are_wrapped(error):
    provider, _ = provider_for({package_url(): error})
    with pytest.raises(SourceError, match="Could not query Thunderstore"):
        provider.resolve(thunder_mod())


def test_http_500_retries_with_injected_sleeper():
    delays = []
    provider, session = provider_for(
        {
            package_url(): [
                FakeResponse({}, 500),
                FakeResponse(package_payload()),
            ]
        },
        attempts=2,
        sleeper=delays.append,
    )
    assert provider.resolve(thunder_mod()).version == "1.0.0"
    assert len(session.calls) == 2
    assert delays == [1.0]


def test_http_429_is_retried_with_a_bound():
    delays = []
    provider, session = provider_for(
        {
            package_url(): [
                FakeResponse({}, 429),
                FakeResponse(package_payload()),
            ]
        },
        attempts=2,
        sleeper=delays.append,
    )
    assert provider.resolve(thunder_mod()).version == "1.0.0"
    assert len(session.calls) == 2
    assert delays == [1.0]


@pytest.mark.parametrize(
    "url",
    [
        "http://thunderstore.io/package/download/A/P/1.0.0/",
        "file:///tmp/package.zip",
        "https://127.0.0.1/package.zip",
        "https://localhost/package.zip",
        "https://evil.test/package.zip",
    ],
)
def test_malicious_download_url_is_rejected(url):
    payload = package_payload()
    payload["latest"]["download_url"] = url
    with pytest.raises(SourceError, match="download URL"):
        resolve_latest(payload)


def test_zip_slip_is_rejected_before_install(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "manifest.json",
            json.dumps({"name": "Package", "version_number": "1.0.0", "dependencies": []}),
        )
        bundle.writestr("../../escaped.txt", b"no")
    provider, _ = provider_for({package_url(): FakeResponse(package_payload())})
    pack = make_pack(tmp_path, [thunder_mod()])
    report = Installer(
        ArchiveDownloader({("Package", "1.0.0"): archive}),
        source_registry=registry_with(provider),
    ).install_modpack(pack)
    assert "Unsafe path" in report.failures[0].message
    assert not (tmp_path / "escaped.txt").exists()


def test_rollback_after_failed_dependency_installation(tmp_path):
    root1 = make_archive(tmp_path / "root1.zip", version="1.0.0")
    root2 = make_archive(
        tmp_path / "root2.zip", version="2.0.0", dependencies=["Deps-Lib-2.0.0"]
    )
    lib2 = make_archive(tmp_path / "lib2.zip", package="Lib", version="2.0.0")
    pack = make_pack(tmp_path, [thunder_mod()])
    provider1, _ = provider_for(
        {package_url(): FakeResponse(package_payload(version="1.0.0"))}
    )
    Installer(
        ArchiveDownloader({("Package", "1.0.0"): root1}),
        source_registry=registry_with(provider1),
    ).install_modpack(pack)
    before = load_state_file(pack.install_directory / ".modsync-state.json")
    routes = {
        package_url(): FakeResponse(
            package_payload(version="2.0.0", dependencies=["Deps-Lib-2.0.0"])
        ),
        package_url("Deps", "Lib"): FakeResponse(
            package_payload("Deps", "Lib", version="2.0.0")
        ),
        version_url("Deps", "Lib", "2.0.0"): FakeResponse(
            version_payload("Deps", "Lib", "2.0.0")
        ),
    }
    provider2, _ = provider_for(routes)
    report = FailOnPackageInstaller(
        "Lib",
        ArchiveDownloader(
            {("Package", "2.0.0"): root2, ("Lib", "2.0.0"): lib2}
        ),
        source_registry=registry_with(provider2),
    ).update_modpack(pack)
    assert report.rollback_succeeded is True
    assert load_state_file(pack.install_directory / ".modsync-state.json") == before
    assert not (pack.install_directory / "Deps-Lib").exists()


def test_profile_keeps_thunderstore_dependency_state_separate(tmp_path):
    config = tmp_path / "pack.json"
    config.write_text(
        json.dumps(
            {
                "name": "Pack",
                "version": "1",
                "game": "Game",
                "install_directory": "mods",
                "mods": [
                    {
                        "name": "ExampleMod",
                        "source": {
                            "type": "thunderstore",
                            "community": "valheim",
                            "namespace": "Author",
                            "package": "Package",
                            "version": "latest",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = ProfileStore(tmp_path / "data")
    store.create("friends", config)
    archive = make_archive(tmp_path / "package.zip")
    provider, _ = provider_for({package_url(): FakeResponse(package_payload())})
    pack = store.load_modpack("friends")
    report = Installer(
        ArchiveDownloader({("Package", "1.0.0"): archive}),
        source_registry=registry_with(provider),
    ).install_modpack(pack)
    assert not report.failures
    assert load_state_file(pack.state_path)["mods"]["ExampleMod"]["version"] == "1.0.0"
    assert not (pack.install_directory / ".modsync-state.json").exists()


def test_info_shows_thunderstore_policy_and_dependency(tmp_path, capsys):
    config = tmp_path / "pack.json"
    config.write_text(
        json.dumps(
            {
                "name": "Pack",
                "version": "1",
                "game": "Game",
                "install_directory": "mods",
                "mods": [
                    {
                        "name": "ExampleMod",
                        "source": {
                            "type": "thunderstore",
                            "community": "valheim",
                            "namespace": "Author",
                            "package": "Package",
                            "version": "latest",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    root_archive = make_archive(
        tmp_path / "root.zip", dependencies=["Deps-Library-1.0.0"]
    )
    dependency_archive = make_archive(
        tmp_path / "dependency.zip", package="Library"
    )
    routes = {
        package_url(): FakeResponse(
            package_payload(dependencies=["Deps-Library-1.0.0"])
        ),
        package_url("Deps", "Library"): FakeResponse(
            package_payload("Deps", "Library")
        ),
        version_url("Deps", "Library"): FakeResponse(
            version_payload("Deps", "Library")
        ),
    }
    provider, _ = provider_for(routes)
    pack = load_modpack(config)
    report = Installer(
        ArchiveDownloader(
            {
                ("Package", "1.0.0"): root_archive,
                ("Library", "1.0.0"): dependency_archive,
            }
        ),
        source_registry=registry_with(provider),
    ).install_modpack(pack)
    assert not report.failures

    assert main(["info", str(config)]) == 0
    output = capsys.readouterr().out
    assert "NAME | SOURCE | VERSION | STATUS" in output
    assert "ExampleMod | source: thunderstore | 1.0.0 | latest" in output
    assert "Deps-Library | source: thunderstore | 1.0.0 | dependency" in output


def test_info_shows_pinned_thunderstore_policy(tmp_path, capsys):
    config = tmp_path / "pack.json"
    config.write_text(
        json.dumps(
            {
                "name": "Pack",
                "version": "1",
                "game": "Game",
                "install_directory": "mods",
                "mods": [
                    {
                        "name": "PinnedMod",
                        "source": {
                            "type": "thunderstore",
                            "community": "valheim",
                            "namespace": "Author",
                            "package": "Package",
                            "version": "1.4.2",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert main(["info", str(config)]) == 0
    output = capsys.readouterr().out
    assert "PinnedMod | source: thunderstore | 1.4.2 | pinned" in output


def test_mixed_direct_github_thunderstore_plan_and_install(tmp_path):
    ts_archive = make_archive(tmp_path / "ts.zip")
    direct_archive = tmp_path / "direct.zip"
    github_archive = tmp_path / "github.zip"
    for archive, content in ((direct_archive, b"direct"), (github_archive, b"github")):
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("plugin.txt", content)
    provider, _ = provider_for({package_url(): FakeResponse(package_payload())})
    github = ResolvedMod(
        name="GitHubMod",
        version="2.0.0",
        download_url="https://github.com/o/r/releases/download/v2/mod.zip",
        filename="github.zip",
        sha256=None,
        source_metadata={"type": "github"},
        release_metadata={},
        source_identity={"type": "github", "tag": "v2"},
    )
    mods = [
        Mod("DirectMod", "1", "https://example.com/direct.zip"),
        Mod(
            "GitHubMod",
            None,
            None,
            source=SourceSpec(
                "github", {"repository": "o/r", "release": "latest", "asset": "x.zip"}
            ),
        ),
        thunder_mod(),
    ]
    downloader = ArchiveDownloader(
        {
            "DirectMod": direct_archive,
            "GitHubMod": github_archive,
            ("Package", "1.0.0"): ts_archive,
        }
    )
    report = Installer(
        downloader,
        source_registry=registry_with(provider, direct=True, github=github),
    ).install_modpack(make_pack(tmp_path, mods))
    assert not report.failures
    assert report.installed == 3


def test_v04_legacy_direct_remains_supported(tmp_path):
    path = tmp_path / "pack.json"
    path.write_text(
        json.dumps(
            {
                "name": "Legacy",
                "version": "1",
                "game": "Game",
                "install_directory": "mods",
                "mods": [
                    {
                        "name": "OldMod",
                        "version": "1.0",
                        "url": "https://example.com/old.zip",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert load_modpack(path).mods[0].source.type == "direct"


def test_request_cache_avoids_duplicate_package_metadata_calls():
    provider, session = provider_for(
        {package_url(): FakeResponse(package_payload())}
    )
    provider.resolve(thunder_mod())
    provider.resolve(thunder_mod())
    assert [call[0] for call in session.calls].count(package_url()) == 1
