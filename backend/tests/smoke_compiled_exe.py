"""
End-to-end smoke test for the PyInstaller-built `waterfree` executable.

Invokes each CLI subcommand against a temp workspace, asserting that the exe:
  * starts without raising a Python import error,
  * exits with the documented exit code,
  * emits parseable JSON to stdout (where the design contract says so).

Designed to run in CI after `build_installer.ps1` (or any equivalent that
produces the exe), and on a fresh machine with no Python on PATH — catching
missing hiddenimports / PyInstaller spec drift before customer install time.

Usage:
    python backend/tests/smoke_compiled_exe.py [--exe PATH]

Exit 0 on success, 1 on any failure. Prints a one-line summary per check.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _default_exe() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    # Try one-dir runtimes first, then legacy one-file locations.
    for candidate in (
        repo_root / "dist" / "waterfree-win32-x64" / "waterfree.exe",
        repo_root / "bin" / "waterfree-win32-x64" / "waterfree.exe",
        repo_root / "dist" / "waterfree-linux-x64" / "waterfree",
        repo_root / "bin" / "waterfree-linux-x64" / "waterfree",
        repo_root / "dist" / "waterfree-darwin-x64" / "waterfree",
        repo_root / "bin" / "waterfree-darwin-x64" / "waterfree",
        repo_root / "dist" / "waterfree-darwin-arm64" / "waterfree",
        repo_root / "bin" / "waterfree-darwin-arm64" / "waterfree",
        repo_root / "dist" / "waterfree-win32-x64.exe",
        repo_root / "bin" / "waterfree-win32-x64.exe",
        repo_root / "dist" / "waterfree-linux-x64",
        repo_root / "dist" / "waterfree-darwin-x64",
        repo_root / "dist" / "waterfree-darwin-arm64",
    ):
        if candidate.exists():
            return candidate
    raise SystemExit("No compiled waterfree exe found. Run build_installer.ps1 first.")


def _run(exe: Path, args: list[str], *, workspace: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [str(exe)] + args
    if workspace is not None:
        cmd += ["--workspace", str(workspace)]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _run_bytes(
    exe: Path, args: list[str], payload: bytes, *, workspace: Path,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(exe)] + args + ["--workspace", str(workspace)],
        input=payload,
        capture_output=True,
        timeout=60,
    )


def _run_powershell_utf8_pipe(
    exe: Path, args: list[str], payload: dict | list, *, workspace: Path,
) -> subprocess.CompletedProcess:
    """Pipe UTF-8 JSON through Windows PowerShell into the installed launcher."""
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        raise RuntimeError("PowerShell is required for this Windows launcher check")
    encoded_payload = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    powershell_args = ", ".join(
        "'" + arg.replace("'", "''") + "'"
        for arg in [*args, "--workspace", str(workspace)]
    )
    script = (
        "$OutputEncoding = [Console]::OutputEncoding = "
        "[System.Text.UTF8Encoding]::new($false); "
        "$payload = [System.Text.Encoding]::UTF8.GetString("
        f"[System.Convert]::FromBase64String('{encoded_payload}')); "
        f"$waterfreeArgs = @({powershell_args}); "
        "$payload | & $env:WATERFREE_TEST_EXE @waterfreeArgs"
    )
    environment = os.environ.copy()
    environment["WATERFREE_TEST_EXE"] = str(exe)
    return subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )


def _check_json(out: str, label: str) -> dict | list:
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{label}: stdout is not valid JSON: {exc}\nstdout was:\n{out[:500]}")


def run_checks(exe: Path) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    # 1. --help works and exits 0.
    r = _run(exe, ["--help"])
    record("waterfree --help", r.returncode == 0, f"exit={r.returncode}")

    with tempfile.TemporaryDirectory(prefix="waterfree-smoke-") as tmp:
        ws = Path(tmp)

        # 2. todos list on a fresh workspace returns valid JSON with 0 tasks.
        r = _run(exe, ["todos", "list"], workspace=ws)
        if r.returncode != 0:
            record("todos list (fresh)", False, f"exit={r.returncode} stderr={r.stderr[:200]}")
        else:
            try:
                data = _check_json(r.stdout, "todos list")
                ok = isinstance(data, dict) and data.get("total") == 0
                record("todos list (fresh)", ok, f"total={data.get('total') if isinstance(data, dict) else '?'}")
            except AssertionError as exc:
                record("todos list (fresh)", False, str(exc))

        # 3. todos add + delete round-trip.
        r = _run(exe, ["todos", "add", "--title", "smoke", "--description", "smoke task", "--priority", "P3"], workspace=ws)
        task_id = None
        if r.returncode == 0:
            try:
                task = _check_json(r.stdout, "todos add")
                task_id = task.get("id") if isinstance(task, dict) else None
                record("todos add", task_id is not None, f"id={task_id}")
            except AssertionError as exc:
                record("todos add", False, str(exc))
        else:
            record("todos add", False, f"exit={r.returncode} stderr={r.stderr[:200]}")

        if task_id:
            r = _run(exe, ["todos", "delete", task_id], workspace=ws)
            record("todos delete", r.returncode == 0, f"exit={r.returncode}")

        # 3b. Windows PowerShell must pass UTF-8 JSON through the installed
        # launcher unchanged for import, add, and update patch inputs.
        if os.name == "nt":
            unicode_title = "Maya’s “Cafe\u0301” — plan"
            import_payload = [{
                "key": "UTF8-IMPORT-001", "title": unicode_title, "description": "Unicode stdin",
            }]
            r_dry = _run_powershell_utf8_pipe(
                exe, ["todos", "import", "--file", "-", "--dry-run"], import_payload, workspace=ws,
            )
            r_import = _run_powershell_utf8_pipe(
                exe, ["todos", "import", "--file", "-"], import_payload, workspace=ws,
            )
            try:
                dry_data = _check_json(r_dry.stdout, "PowerShell UTF-8 import dry-run")
                import_data = _check_json(r_import.stdout, "PowerShell UTF-8 import")
                import_ok = (
                    r_dry.returncode == 0 and r_import.returncode == 0
                    and dry_data["created"][0]["title"] == unicode_title
                    and import_data["created"][0]["title"] == unicode_title
                )
                record("PowerShell UTF-8 import dry-run parity", import_ok,
                       f"dry={r_dry.returncode} import={r_import.returncode}")
            except (AssertionError, KeyError, IndexError, TypeError) as exc:
                record("PowerShell UTF-8 import dry-run parity", False, str(exc))

            r_add = _run_powershell_utf8_pipe(
                exe, ["todos", "add", "--json-file", "-"],
                {"title": "Maya’s “Cafe\u0301” — add", "description": "Unicode stdin"}, workspace=ws,
            )
            try:
                added = _check_json(r_add.stdout, "PowerShell UTF-8 add")
                add_ok = r_add.returncode == 0 and added["title"] == "Maya’s “Cafe\u0301” — add"
                record("PowerShell UTF-8 add", add_ok, f"exit={r_add.returncode}")
            except (AssertionError, KeyError, TypeError) as exc:
                added = {}
                record("PowerShell UTF-8 add", False, str(exc))

            r_patch = _run_powershell_utf8_pipe(
                exe, ["todos", "update", str(added.get("id", "")), "--patch-file", "-"],
                {"title": "Maya’s “Cafe\u0301” — patch"}, workspace=ws,
            )
            try:
                patched = _check_json(r_patch.stdout, "PowerShell UTF-8 patch")
                patch_ok = r_patch.returncode == 0 and patched["title"] == "Maya’s “Cafe\u0301” — patch"
                record("PowerShell UTF-8 patch", patch_ok, f"exit={r_patch.returncode}")
            except (AssertionError, KeyError, TypeError) as exc:
                record("PowerShell UTF-8 patch", False, str(exc))

            malformed = _run_bytes(
                exe,
                ["todos", "import", "--file", "-", "--dry-run"],
                b'[{"title":"bad \xff", "description":"d"}]',
                workspace=ws,
            )
            malformed_error = malformed.stderr.decode("utf-8", errors="replace")
            malformed_ok = (
                malformed.returncode == 2
                and malformed_error.startswith("error: --file")
                and "Traceback" not in malformed_error
            )
            record("installed CLI rejects malformed UTF-8", malformed_ok,
                   f"exit={malformed.returncode}")
        else:
            record("PowerShell UTF-8 stdin checks", True, "not applicable")

        # 4. knowledge stats (global, no --workspace).
        r = _run(exe, ["knowledge", "stats"])
        if r.returncode == 0:
            try:
                data = _check_json(r.stdout, "knowledge stats")
                ok = isinstance(data, dict) and "total_entries" in data
                record("knowledge stats", ok, f"total_entries={data.get('total_entries')}")
            except AssertionError as exc:
                record("knowledge stats", False, str(exc))
        else:
            record("knowledge stats", False, f"exit={r.returncode} stderr={r.stderr[:200]}")

        # 5. index status on a fresh (not-indexed) workspace should still return JSON.
        r = _run(exe, ["index", "status"], workspace=ws)
        if r.returncode == 0:
            try:
                _check_json(r.stdout, "index status")
                record("index status (fresh)", True)
            except AssertionError as exc:
                record("index status (fresh)", False, str(exc))
        else:
            # Acceptable if the dep-missing exit code is used.
            record("index status (fresh)", r.returncode in (0, 4), f"exit={r.returncode}")

        # 6. index build with enough files to exercise graphify's process pool.
        index_ws = ws / "indexable"
        index_ws.mkdir()
        for i in range(25):
            (index_ws / f"module_{i:02d}.py").write_text(
                f"def func_{i:02d}():\n    return {i}\n",
                encoding="utf-8",
            )
        r = _run(exe, ["index", "build"], workspace=index_ws)
        if r.returncode == 0:
            try:
                data = _check_json(r.stdout, "index build")
                ok = (
                    isinstance(data, dict)
                    and data.get("files_indexed", 0) > 0
                    and data.get("nodes", 0) > 0
                )
                record(
                    "index build (process pool)",
                    ok,
                    f"files={data.get('files_indexed') if isinstance(data, dict) else '?'} "
                    f"nodes={data.get('nodes') if isinstance(data, dict) else '?'}",
                )
            except AssertionError as exc:
                record("index build (process pool)", False, str(exc))
        else:
            record("index build (process pool)", False, f"exit={r.returncode} stderr={r.stderr[:300]}")

        # 7. testing list on a fresh workspace - empty array but valid JSON.
        r = _run(exe, ["testing", "list"], workspace=ws)
        if r.returncode == 0:
            try:
                data = _check_json(r.stdout, "testing list")
                record("testing list (fresh)", isinstance(data, list), f"len={len(data) if isinstance(data, list) else '?'}")
            except AssertionError as exc:
                record("testing list (fresh)", False, str(exc))
        else:
            record("testing list (fresh)", False, f"exit={r.returncode} stderr={r.stderr[:200]}")

        # 8. Unknown subcommand returns usage exit code (2).
        r = _run(exe, ["bogus", "action"], workspace=ws)
        record("bogus subcommand -> usage error", r.returncode in (1, 2), f"exit={r.returncode}")

    return results


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--exe", type=Path, default=None,
                   help="Path to the compiled exe. Defaults to dist/ or bin/.")
    args = p.parse_args()

    exe = args.exe or _default_exe()
    if not exe.exists():
        print(f"FAIL: exe not found: {exe}", file=sys.stderr)
        return 1

    print(f"Smoke-testing: {exe}")
    print(f"Size:          {exe.stat().st_size / (1024*1024):.1f} MB")
    print()

    results = run_checks(exe)
    ok = sum(1 for _, passed, _ in results if passed)
    failed = sum(1 for _, passed, _ in results if not passed)

    for name, passed, detail in results:
        marker = "PASS" if passed else "FAIL"
        line = f"  [{marker}] {name}"
        if detail:
            line += f"  ({detail})"
        print(line)

    print()
    print(f"{ok} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
