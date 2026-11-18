"""Engines that are Python projects: a pinned uv-built venv plus weights in the Hugging Face cache.

trellis.cpp and openmoss ship prebuilt binaries (:mod:`backend.engines.catalog`).
Kimodo (animgen) and the audio QA models (faster-whisper + CLAP) are PyTorch
code, and each pins library versions WaterFree's own venv cannot share, so each
gets a dedicated venv at ``engines/<key>/<version>/venv``:

* ``install`` builds it with uv (Python and torch from pinned specs, then each
  package stage in order), checks it can import what the engine needs, fetches
  the weights into the HF cache (files already there are kept), and records the
  revisions it got in ``.waterfree-install.json``.
* ``status`` answers from the marker and the cache layout, without starting the
  venv: a weight repo whose ``refs/main`` moved since install counts as stale,
  because engines run offline and resolve "main" from the cache.
* Engine processes get :func:`offline_env`: ``HF_HUB_OFFLINE=1``, so running a
  job never downloads and never needs a token.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from backend.app_paths import engines_dir
from backend.engines.install import InstallError, disk_free_gb

MARKER = ".waterfree-install.json"
FETCH_SCRIPT = Path(__file__).with_name("hf_fetch.py")

Report = Callable[[str], None]


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@dataclass(frozen=True)
class HfRepo:
    repo: str
    revision: str            # the commit this release was built and measured against
    files: tuple = ()        # exact files to fetch; empty = the whole repo
    required: tuple = ()     # files whose presence means "installed"
    gated: bool = False
    licence: str = ""
    size_gb: float = 0.0


@dataclass(frozen=True)
class PythonEngine:
    key: str
    project: str
    version: str
    python: str
    torch: str
    torch_index: str
    stages: tuple            # package groups, installed in order (a later stage may build with an earlier one)
    check_imports: tuple     # modules the venv must import for the engine to work
    repos: tuple
    licence: str
    venv_gb: float
    needs_compiler: bool = False
    commit: str = ""
    notes: dict = field(default_factory=dict)

    def install_dir(self) -> Path:
        return engines_dir() / self.key / self.version

    def venv_dir(self) -> Path:
        return self.install_dir() / "venv"

    def python_exe(self) -> Path:
        if sys.platform == "win32":
            return self.venv_dir() / "Scripts" / "python.exe"
        return self.venv_dir() / "bin" / "python"

    def repo(self, name: str) -> HfRepo:
        return next(r for r in self.repos if r.repo == name)

    def spec(self) -> dict:
        """What the venv must contain; a marker that differs means rebuild."""
        return {"python": self.python, "torch": self.torch, "torchIndex": self.torch_index,
                "packages": [package for stage in self.stages for package in stage]}


# ---------------------------------------------------------------------------
# Hugging Face cache layout (read directly: WaterFree's venv need not import the hub)
# ---------------------------------------------------------------------------

def hf_hub_cache() -> Path:
    for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(name, "").strip():
            return Path(os.environ[name]).expanduser()
    home = os.environ.get("HF_HOME", "").strip()
    if not home:
        xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
        home = str(Path(xdg) / "huggingface") if xdg else str(Path.home() / ".cache" / "huggingface")
    return Path(home).expanduser() / "hub"


def _repo_folder(repo: str) -> Path:
    return hf_hub_cache() / ("models--" + repo.replace("/", "--"))


def cached_main(repo: str) -> str | None:
    try:
        return (_repo_folder(repo) / "refs" / "main").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def snapshot_dir(repo: str, revision: str) -> Path:
    return _repo_folder(repo) / "snapshots" / revision


def repo_status(entry: HfRepo, installed_revision: str | None) -> dict:
    main = cached_main(entry.repo)
    revision = installed_revision or main
    missing = list(entry.required)
    if revision:
        snapshot = snapshot_dir(entry.repo, revision)
        missing = [name for name in entry.required if not (snapshot / name).is_file()]
    # Engines run offline and resolve "main" from refs/main. If another tool moved
    # it since install, the revision recorded in sidecars would be wrong: reinstall.
    moved = bool(installed_revision and main and main != installed_revision)
    return {
        "repo": entry.repo,
        "revision": revision,
        "pinned": entry.revision,
        "present": bool(revision) and not missing and not moved,
        "missing": missing,
        "movedSinceInstall": moved,
        "sizeGb": entry.size_gb,
        "licence": entry.licence,
    }


# ---------------------------------------------------------------------------
# Status / require
# ---------------------------------------------------------------------------

def read_marker(engine: PythonEngine) -> dict:
    try:
        data = json.loads((engine.install_dir() / MARKER).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def status(engine: PythonEngine) -> dict:
    marker = read_marker(engine)
    current = marker.get("version") == engine.version
    revisions = marker.get("revisions", {}) if current else {}
    venv_ready = (engine.python_exe().is_file() and current and marker.get("commit", "") == engine.commit
                  and marker.get("spec") == engine.spec())
    repos = [repo_status(entry, revisions.get(entry.repo)) for entry in engine.repos]
    return {
        "key": engine.key,
        "project": engine.project,
        "version": engine.version,
        "installed": venv_ready and all(r["present"] for r in repos),
        "venvReady": venv_ready,
        "path": str(engine.install_dir()),
        "python": str(engine.python_exe()),
        "weights": repos,
        "hfCache": str(hf_hub_cache()),
        "licence": engine.licence,
    }


def require(engine: PythonEngine, *, install_hint: str) -> dict:
    """The install marker, or raise InstallError naming what is missing and the fix."""
    current = status(engine)
    if current["installed"]:
        return read_marker(engine)
    problems = []
    if not current["venvReady"]:
        problems.append(f"the {engine.key} {engine.version} venv is not installed")
    for repo in current["weights"]:
        if repo["movedSinceInstall"]:
            problems.append(f"{repo['repo']} changed in the Hugging Face cache since install")
        elif not repo["present"]:
            problems.append(f"{repo['repo']} is not in the Hugging Face cache")
    raise InstallError(f"{'; '.join(problems)}. Install with: {install_hint}")


def offline_env(extra: dict | None = None) -> dict:
    """Environment for an engine process: offline HF, quiet, unbuffered UTF-8 output."""
    env = dict(os.environ)
    env.update({
        "HF_HUB_OFFLINE": "1",              # running a job never downloads; install did that
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_SYMLINKS": "1",     # see todo GPU-010 (huggingface_hub symlink-probe race)
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "TQDM_DISABLE": "1",                # engines log their own progress lines
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    env.update(extra or {})
    return env


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install(engine: PythonEngine, *, report: Report = _stderr) -> dict:
    """Create/refresh the venv and fetch the weights. Idempotent; nothing is fetched twice."""
    actions: dict[str, str] = {}
    marker = read_marker(engine)
    current = status(engine)
    _check_disk(engine, current)

    check = _check_venv(engine, report=None) if current["venvReady"] else None
    if check is not None:
        actions["venv"] = "present"
    else:
        _check_prerequisites(engine)
        _build_venv(engine, report)
        actions["venv"] = "installed"
        check = _check_venv(engine, report=report)
        if check is None:
            raise InstallError(f"The {engine.key} venv at {engine.venv_dir()} was built but cannot import "
                               f"{', '.join(engine.check_imports)}; see the output above.")
    marker = {**marker, "engine": engine.key, "project": engine.project, "version": engine.version,
              "commit": engine.commit, "spec": engine.spec(), "check": check}
    _write_marker(engine, marker)

    fetched = _fetch_weights(engine, report)
    revisions = {item["repo"]: item["revision"] for item in fetched}
    changed = [f"{repo} is at {rev[:12]}, not the tested {engine.repo(repo).revision[:12]}"
               for repo, rev in revisions.items() if rev != engine.repo(repo).revision]
    for note in changed:
        report(f"note: {note} (upstream moved; recorded in every sidecar)")
    marker = {**marker, "revisions": revisions, "installedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _write_marker(engine, marker)
    # The hub does not say whether it downloaded or found the files; either way they are now in the cache.
    return {**status(engine), "actions": {**actions, **{item["repo"]: "in HF cache" for item in fetched}},
            "notes": changed, "check": check}


def _write_marker(engine: PythonEngine, data: dict) -> None:
    engine.install_dir().mkdir(parents=True, exist_ok=True)
    (engine.install_dir() / MARKER).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _check_disk(engine: PythonEngine, current: dict) -> None:
    need = 0.0 if current["venvReady"] else engine.venv_gb
    need += sum(r["sizeGb"] for r in current["weights"] if not r["present"])
    if need <= 0:
        return
    for where in {engine.install_dir(), hf_hub_cache()}:
        free = disk_free_gb(where)
        if free < need + 2:
            raise InstallError(f"Installing {engine.key} needs up to {need:.0f} GB on the drive holding {where}, "
                               f"but only {free:.0f} GB is free.")


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "uv.exe", Path.home() / ".cargo" / "bin" / "uv.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def msvc_available() -> bool:
    if sys.platform != "win32":
        return shutil.which("c++") is not None or shutil.which("g++") is not None
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / \
        "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        return False
    result = subprocess.run([str(vswhere), "-products", "*", "-requires",
                             "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
                            capture_output=True, text=True, check=False, timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return bool(result.stdout.strip())


def _check_prerequisites(engine: PythonEngine) -> None:
    problems = []
    if not find_uv():
        problems.append("uv is not installed (it builds the engine's venv): `winget install astral-sh.uv`, "
                        "or see https://docs.astral.sh/uv/")
    if engine.needs_compiler and not msvc_available():
        problems.append("no C++ compiler found. This engine compiles a small extension: install Visual Studio "
                        "Build Tools with the \"Desktop development with C++\" workload" if sys.platform == "win32"
                        else "no C++ compiler found (install g++ and make)")
    if problems:
        raise InstallError(f"Cannot build the {engine.key} engine: " + "; ".join(problems) + ".")


def _build_venv(engine: PythonEngine, report: Report) -> None:
    uv = find_uv()
    python = str(engine.python_exe())
    if not engine.python_exe().is_file():
        report(f"{engine.key}: creating a Python {engine.python} venv at {engine.venv_dir()}")
        _run([uv, "venv", "--python", engine.python, str(engine.venv_dir())], report)
    report(f"{engine.key}: installing {engine.torch} (CUDA build)")
    _run([uv, "pip", "install", "--python", python, engine.torch, "--index-url", engine.torch_index], report)
    env = dict(os.environ)
    # Build steps (e.g. Kimodo's setup.py running `cmake`) look on PATH: the venv's own tools first.
    env["PATH"] = str(Path(python).parent) + os.pathsep + env.get("PATH", "")
    for stage in engine.stages:
        report(f"{engine.key}: installing {', '.join(stage)}")
        _run([uv, "pip", "install", "--python", python, *stage], report, env=env)


def _check_venv(engine: PythonEngine, *, report: Report | None) -> dict | None:
    if not engine.python_exe().is_file():
        return None
    result = subprocess.run([str(engine.python_exe()), "-I", str(FETCH_SCRIPT), "check", *engine.check_imports],
                            capture_output=True, text=True, check=False, timeout=600, env=offline_env(),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        if report:
            report(result.stderr.strip()[-2000:])
        return None
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def _fetch_weights(engine: PythonEngine, report: Report) -> list[dict]:
    if not engine.repos:
        return []
    spec = [{"repo": r.repo, "files": list(r.files)} for r in engine.repos]
    report(f"{engine.key}: fetching weights into the Hugging Face cache (files already there are kept)")
    with tempfile.TemporaryDirectory(prefix=f"waterfree-{engine.key}-") as folder:
        spec_path = Path(folder) / "spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        env = offline_env()
        for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "TQDM_DISABLE"):
            env.pop(name, None)  # this step is the one that downloads (with progress bars)
        lines = _run([str(engine.python_exe()), "-I", str(FETCH_SCRIPT), "fetch", str(spec_path)], report,
                     env=env, check=False, capture_json=True)
    results = [line for line in lines if isinstance(line, dict)]
    errors = [r for r in results if r.get("error")]
    if errors:
        messages = []
        for error in errors:
            if error["error"] in ("gated", "auth"):
                messages.append(
                    f"{error['repo']} is gated: accept its licence at https://huggingface.co/{error['repo']}, "
                    "create a read token at https://huggingface.co/settings/tokens, and set HF_TOKEN "
                    "(or run `hf auth login`), then install again")
            else:
                messages.append(f"{error['repo']}: {error.get('type')}: {error.get('message')}")
        raise InstallError(f"Could not fetch the {engine.key} weights: " + "; ".join(messages))
    if len(results) != len(engine.repos):
        raise InstallError("The weight download ended early; run install again (finished files are kept).")
    return results


def _run(argv: list[str], report: Report, *, env: dict | None = None, check: bool = True,
         capture_json: bool = False) -> list:
    """Run a child, relaying its output as progress; JSON lines are returned when asked for."""
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                               stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    parsed: list = []
    tail: list[str] = []
    for raw in process.stdout:
        line = raw.decode(errors="replace").rstrip()
        if not line:
            continue
        if capture_json and line.startswith("{"):
            try:
                parsed.append(json.loads(line))
                continue
            except ValueError:
                pass
        tail = (tail + [line])[-30:]
        report(f"  {line}")
    code = process.wait()
    if check and code != 0:
        command = " ".join([Path(argv[0]).name, *argv[1:3]])
        raise InstallError(f"`{command} ...` failed with exit code {code}:\n" + "\n".join(tail[-15:]))
    return parsed
