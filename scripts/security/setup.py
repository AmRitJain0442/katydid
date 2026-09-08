"""Install exact scanner binaries and refresh the explicit Trivy database cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UV_VERSION = (ROOT / ".uv-version").read_text(encoding="utf-8").strip()
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

ASSETS = {
    ("Windows", "AMD64", "trivy"): (
        "trivy_0.74.0_windows-64bit.zip",
        "94c40e0696e4b907a74b7b2e1438d5d72ebaca83115817407f568a002d520842",
        "https://github.com/aquasecurity/trivy/releases/download/v0.74.0/",
    ),
    ("Linux", "x86_64", "trivy"): (
        "trivy_0.74.0_Linux-64bit.tar.gz",
        "2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a",
        "https://github.com/aquasecurity/trivy/releases/download/v0.74.0/",
    ),
    ("Windows", "AMD64", "gitleaks"): (
        "gitleaks_8.30.1_windows_x64.zip",
        "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e",
        "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/",
    ),
    ("Linux", "x86_64", "gitleaks"): (
        "gitleaks_8.30.1_linux_x64.tar.gz",
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
        "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/",
    ),
}
VERSIONS = {"semgrep": "1.176.1", "trivy": "0.74.0", "gitleaks": "8.30.1"}


class SetupError(RuntimeError):
    pass


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, expected_sha256: str, destination: Path) -> None:
    digest = hashlib.sha256()
    total = 0
    temporary = destination.with_suffix(destination.suffix + ".download")
    temporary.unlink(missing_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise SetupError("scanner archive exceeds 200 MiB")
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected_sha256:
            raise SetupError("scanner archive checksum does not match the pinned release")
        os.replace(temporary, destination)
    except (OSError, urllib.error.URLError) as exc:
        raise SetupError("scanner archive download failed") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _extract_binary(archive: Path, name: str, destination: Path) -> None:
    expected = name + (".exe" if os.name == "nt" else "")
    data: bytes | None = None
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as source:
            zip_matches = [
                item for item in source.infolist() if Path(item.filename).name == expected
            ]
            if len(zip_matches) == 1 and zip_matches[0].file_size <= MAX_DOWNLOAD_BYTES:
                data = source.read(zip_matches[0])
    else:
        with tarfile.open(archive, "r:gz") as source:
            tar_matches = [item for item in source.getmembers() if Path(item.name).name == expected]
            if (
                len(tar_matches) == 1
                and tar_matches[0].isfile()
                and tar_matches[0].size <= MAX_DOWNLOAD_BYTES
            ):
                stream = source.extractfile(tar_matches[0])
                data = stream.read() if stream is not None else None
    if data is None:
        raise SetupError("scanner archive does not contain exactly one expected binary")
    temporary = destination.with_suffix(destination.suffix + ".install")
    temporary.write_bytes(data)
    temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(temporary, destination)


def _run_quiet(argv: list[str], timeout: int) -> None:
    try:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError("security tool setup command could not run") from exc
    if result.returncode != 0:
        raise SetupError("security tool setup command failed")


def _paths(directory: Path) -> dict[str, Path]:
    suffix = ".exe" if os.name == "nt" else ""
    semgrep = (
        directory / "semgrep" / ("Scripts" if os.name == "nt" else "bin") / ("semgrep" + suffix)
    )
    return {
        "semgrep": semgrep,
        "trivy": directory / "bin" / ("trivy" + suffix),
        "gitleaks": directory / "bin" / ("gitleaks" + suffix),
        "cache": directory / "trivy-cache-unpublished",
    }


def install(directory: Path) -> dict[str, Path]:
    directory = directory.resolve()
    (directory / "bin").mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["UV_PROJECT_ENVIRONMENT"] = str(directory / "semgrep")
    try:
        result = subprocess.run(
            [
                "uvx",
                "--from",
                f"uv=={UV_VERSION}",
                "uv",
                "sync",
                "--project",
                str(Path(__file__).parent),
                "--locked",
                "--no-install-project",
            ],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError("locked Semgrep environment installation failed") from exc
    if result.returncode != 0:
        raise SetupError("locked Semgrep environment installation failed")

    system = platform.system()
    machine = platform.machine()
    paths = _paths(directory)
    with tempfile.TemporaryDirectory(prefix="katydid-security-download-") as temporary:
        temporary_root = Path(temporary)
        for tool in ("trivy", "gitleaks"):
            asset = ASSETS.get((system, machine, tool))
            if asset is None:
                raise SetupError("security setup supports Windows AMD64 and Linux x86_64")
            filename, checksum, prefix = asset
            archive = temporary_root / filename
            _download(prefix + filename, checksum, archive)
            _extract_binary(archive, tool, paths[tool])
    for tool in ("semgrep", "trivy", "gitleaks"):
        if not paths[tool].is_file():
            raise SetupError("security tool installation is incomplete")
    return paths


def _new_cache(directory: Path) -> Path:
    parent = directory.resolve() / "trivy-caches"
    parent.mkdir(parents=True, exist_ok=True)
    generation = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
    cache = parent / generation
    cache.mkdir()
    return cache


def refresh_database(paths: dict[str, Path]) -> dict[str, str]:
    _run_quiet(
        [
            str(paths["trivy"]),
            "image",
            "--download-db-only",
            "--cache-dir",
            str(paths["cache"]),
            "--no-progress",
            "--disable-telemetry",
        ],
        300,
    )
    database = paths["cache"] / "db" / "trivy.db"
    metadata = paths["cache"] / "db" / "metadata.json"
    if not database.is_file() or not metadata.is_file():
        raise SetupError("Trivy database refresh did not produce a complete cache")
    return {
        "refreshed_at": datetime.now(UTC).isoformat(),
        "database_sha256": _sha256(database),
        "metadata_sha256": _sha256(metadata),
    }


def _metadata(paths: dict[str, Path], database: dict[str, str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "prepared_at": datetime.now(UTC).isoformat(),
        "versions": VERSIONS,
        "executables": {name: str(paths[name]) for name in VERSIONS},
        "executable_sha256": {name: _sha256(paths[name]) for name in VERSIONS},
        "trivy_cache": str(paths["cache"]),
        "trivy_database": database,
    }


def prepare(directory: Path) -> dict[str, object]:
    paths = install(directory)
    paths["cache"] = _new_cache(directory)
    result = _metadata(paths, refresh_database(paths))
    _write_json(directory.resolve() / "setup.json", result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare Katydid's pinned security scanners")
    parser.add_argument("action", choices=("prepare", "refresh-db"))
    parser.add_argument("--directory", type=Path, default=ROOT / ".katydid" / "security-tools")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "prepare":
            result = prepare(args.directory)
        else:
            paths = _paths(args.directory.resolve())
            paths["cache"] = _new_cache(args.directory)
            result = _metadata(paths, refresh_database(paths))
            _write_json(args.directory.resolve() / "setup.json", result)
        print(json.dumps(result, indent=2))
        return 0
    except SetupError as exc:
        print(f"security setup: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
