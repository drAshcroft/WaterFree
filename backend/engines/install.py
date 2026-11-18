"""Download, verify and register engines and model weights -- once.

Installs are explicit (`waterfree meshgen install`, `waterfree audiogen install`)
and idempotent:

* A file already at its destination with the right size is *adopted* after its
  SHA-256 checks out; nothing is fetched twice. That also covers weights copied
  or moved in by hand.
* Each install folder carries ``.waterfree-install.json`` recording every
  verified file's size and mtime, so `status` answers in milliseconds instead of
  re-hashing 10 GB, while a file replaced behind our back still shows as
  unverified.
* Downloads resume from ``*.part`` files after an interruption.
* Release archives are untrusted input: each member is checked to extract
  inside the destination before anything is written (no "zip slip").
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

from backend.engines.catalog import (
    ENGINES,
    MODEL_SETS,
    EngineRelease,
    ModelSet,
    RemoteFile,
)

MARKER = ".waterfree-install.json"
_CHUNK = 4 * 1024 * 1024
_REPORT_EVERY = 5.0
_USER_AGENT = "waterfree-engine-installer"

Report = Callable[[str], None]


class InstallError(RuntimeError):
    pass


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Status (fast: marker + size/mtime, no hashing)
# ---------------------------------------------------------------------------

def _read_marker(folder: Path) -> dict:
    try:
        data = json.loads((folder / MARKER).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_marker(folder: Path, data: dict) -> None:
    (folder / MARKER).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _stat_key(path: Path) -> list[int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return [stat.st_size, stat.st_mtime_ns]


def model_status(model_set: ModelSet) -> dict:
    folder = model_set.install_dir()
    verified = _read_marker(folder).get("files", {})
    missing, unverified = [], []
    for remote in model_set.files:
        path = folder / remote.name
        key = _stat_key(path)
        if key is None or key[0] != remote.size:
            missing.append(remote.name)
        elif verified.get(remote.name, {}).get("stat") != key or \
                verified.get(remote.name, {}).get("sha256") != remote.sha256:
            unverified.append(remote.name)
    return {
        "key": model_set.key,
        "label": model_set.label,
        "installed": not missing and not unverified,
        "path": str(folder),
        "sizeGb": round(model_set.size_bytes / 1024 ** 3, 2),
        "vramGb": model_set.vram_gb,
        "licence": model_set.licence,
        "missing": missing,
        "unverified": unverified,
    }


def engine_status(release: EngineRelease) -> dict:
    folder = release.install_dir()
    marker = _read_marker(folder)
    executables = {role: str(release.executable(role)) for role in release.executables}
    present = all(Path(p).is_file() for p in executables.values())
    return {
        "key": release.key,
        "project": release.project,
        "version": release.version,
        "installed": present and marker.get("archiveSha256") == release.archive.sha256,
        "path": str(folder),
        "executables": executables,
        "licence": release.licence,
    }


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install_engine(release: EngineRelease, *, report: Report = _stderr) -> dict:
    status = engine_status(release)
    if status["installed"]:
        report(f"{release.key} {release.version}: already installed at {status['path']}")
        return {**status, "action": "present"}
    root = release.install_dir().parent
    root.mkdir(parents=True, exist_ok=True)
    archive = root / "_downloads" / release.archive.name
    fetch(release.archive, archive, report=report, label=f"{release.key} {release.version}")
    staging = root / f".{release.version}.extracting"
    if staging.exists():
        shutil.rmtree(staging)
    report(f"{release.key}: extracting {archive.name}")
    safe_extract(archive, staging)
    target = release.install_dir()
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    for role in release.executables:
        if not release.executable(role).is_file():
            raise InstallError(f"{release.archive.name} did not contain {release.executables[role]}")
    _write_marker(target, {
        "engine": release.key,
        "project": release.project,
        "version": release.version,
        "archive": release.archive.name,
        "archiveSha256": release.archive.sha256,
        "installedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    # The extracted folder is the install; the archive would only be a second copy.
    archive.unlink(missing_ok=True)
    _rmdir_if_empty(archive.parent)
    return {**engine_status(release), "action": "installed"}


def install_models(model_set: ModelSet, *, report: Report = _stderr) -> dict:
    folder = model_set.install_dir()
    folder.mkdir(parents=True, exist_ok=True)
    marker = _read_marker(folder)
    verified = dict(marker.get("files", {}))
    actions: dict[str, str] = {}
    for remote in model_set.files:
        path = folder / remote.name
        record = verified.get(remote.name, {})
        if record.get("stat") == _stat_key(path) and record.get("sha256") == remote.sha256:
            actions[remote.name] = "present"
            continue
        actions[remote.name] = fetch(remote, path, report=report, label=model_set.key)
        verified[remote.name] = {"sha256": remote.sha256, "stat": _stat_key(path)}
        # Record progress per file so an interrupted install never re-verifies finished files.
        _write_marker(folder, {"set": model_set.key, "repo": model_set.repo,
                               "revision": model_set.revision, "files": verified})
    _write_marker(folder, {
        "set": model_set.key,
        "repo": model_set.repo,
        "revision": model_set.revision,
        "files": verified,
        "installedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    return {**model_status(model_set), "files": actions}


def fetch(remote: RemoteFile, dest: Path, *, report: Report = _stderr, label: str = "") -> str:
    """Make ``dest`` hold ``remote``; return "adopted" or "downloaded"."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{label}: " if label else ""
    if dest.is_file() and dest.stat().st_size == remote.size:
        report(f"{prefix}verifying existing {dest.name} ({_mb(remote.size)} MB)")
        if sha256_file(dest, report=report, label=f"{prefix}{dest.name}") == remote.sha256:
            return "adopted"
        bad = dest.with_name(dest.name + ".corrupt")
        dest.replace(bad)
        report(f"{prefix}{dest.name} failed verification; moved aside to {bad.name} and re-downloading")
    part = dest.with_name(dest.name + ".part")
    _download(remote, part, report=report, label=f"{prefix}{dest.name}")
    report(f"{prefix}verifying {dest.name}")
    digest = sha256_file(part, report=report, label=f"{prefix}{dest.name}")
    if digest != remote.sha256:
        part.unlink(missing_ok=True)
        raise InstallError(
            f"{dest.name}: SHA-256 mismatch (expected {remote.sha256}, got {digest}). "
            "The download was discarded; run install again.")
    part.replace(dest)
    return "downloaded"


def _download(remote: RemoteFile, part: Path, *, report: Report, label: str) -> None:
    for attempt in range(1, 6):
        have = part.stat().st_size if part.exists() else 0
        if have > remote.size:
            part.unlink()
            have = 0
        if have == remote.size:
            return
        headers = {"User-Agent": _USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        request = urllib.request.Request(remote.url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                if have and resp.status != 206:
                    have = 0  # server ignored the range; start over
                with open(part, "ab" if have else "wb") as out:
                    _copy_with_progress(resp, out, have, remote.size, report, label)
            if part.stat().st_size == remote.size:
                return
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            if attempt == 5:
                raise InstallError(f"{label}: download failed after {attempt} attempts: {exc}") from exc
            report(f"{label}: {exc}; retrying ({attempt}/5)")
            time.sleep(min(30, 2 ** attempt))
    raise InstallError(f"{label}: download ended at {part.stat().st_size} of {remote.size} bytes")


def _copy_with_progress(resp, out, have: int, total: int, report: Report, label: str) -> None:
    started = time.monotonic()
    last = started
    done = have
    while True:
        chunk = resp.read(_CHUNK)
        if not chunk:
            break
        out.write(chunk)
        done += len(chunk)
        now = time.monotonic()
        if now - last >= _REPORT_EVERY:
            rate = (done - have) / max(1e-6, now - started) / 1024 ** 2
            report(f"{label}: {_mb(done)}/{_mb(total)} MB ({rate:.1f} MB/s)")
            last = now


def sha256_file(path: Path, *, report: Report | None = None, label: str = "") -> str:
    digest = hashlib.sha256()
    size = path.stat().st_size
    done = 0
    last = time.monotonic()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            if report and size > 1024 ** 3 and time.monotonic() - last >= _REPORT_EVERY:
                report(f"{label}: hashed {_mb(done)}/{_mb(size)} MB")
                last = time.monotonic()
    return digest.hexdigest()


def safe_extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (dest / member.filename).resolve()
            if target != root and root not in target.parents:
                raise InstallError(f"{archive.name}: member {member.filename!r} would extract outside "
                                   "the install folder; refusing the archive.")
        bundle.extractall(dest)


def _rmdir_if_empty(folder: Path) -> None:
    try:
        folder.rmdir()
    except OSError:
        pass


def _mb(size: int) -> int:
    return int(size / 1024 ** 2)


# ---------------------------------------------------------------------------
# Lookups used by the areas
# ---------------------------------------------------------------------------

def require(engine_key: str, model_key: str, *, install_hint: str) -> tuple[EngineRelease, ModelSet]:
    """The engine and model set, or raise InstallError naming the command that installs them."""
    release = ENGINES[engine_key]
    model_set = MODEL_SETS[model_key]
    problems = []
    if not engine_status(release)["installed"]:
        problems.append(f"engine {release.key} {release.version} is not installed")
    status = model_status(model_set)
    if not status["installed"]:
        parts = []
        if status["missing"]:
            parts.append(f"missing {', '.join(status['missing'])}")
        if status["unverified"]:
            parts.append(f"unverified {', '.join(status['unverified'])}")
        problems.append(f"model set {model_set.key} is incomplete ({'; '.join(parts)})")
    if problems:
        raise InstallError(f"{'; '.join(problems)}. Install with: {install_hint}")
    return release, model_set


def disk_free_gb(path: Path) -> float:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free / 1024 ** 3


def needed_bytes(model_sets: list[ModelSet], releases: list[EngineRelease]) -> int:
    total = 0
    for model_set in model_sets:
        status = model_status(model_set)
        for remote in model_set.files:
            if remote.name in status["missing"]:
                total += remote.size
    for release in releases:
        if not engine_status(release)["installed"]:
            total += release.archive.size * 2  # archive + extracted copy while unpacking
    return total
