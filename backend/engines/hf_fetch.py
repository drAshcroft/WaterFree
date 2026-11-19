"""Install helper run *inside an engine venv* (``python -I hf_fetch.py ...``); standard library at import.

    hf_fetch.py fetch SPEC_JSON          # [{"repo", "files"?}] -> one JSON line per repo
    hf_fetch.py check MODULE [MODULE...] # import each; print versions as one JSON line

Each Python engine (Kimodo, audio QA) has its own venv with its own
``huggingface_hub``; downloading through that one keeps the HF cache format
the engine will read. WaterFree's process only reads the JSON lines.
"""

from __future__ import annotations

import json
import os
import sys


def fetch(spec_path: str) -> int:
    from huggingface_hub import snapshot_download

    with open(spec_path, encoding="utf-8") as handle:
        spec = json.load(handle)
    failed = False
    for item in spec:
        repo = item["repo"]
        try:
            path = snapshot_download(repo_id=repo, allow_patterns=item.get("files") or None)
            result = {"repo": repo, "revision": os.path.basename(os.path.normpath(path)), "path": path}
        except Exception as exc:  # noqa: BLE001 - classified for the parent, which owns the message
            failed = True
            name = type(exc).__name__
            text = str(exc)
            kind = "gated" if name == "GatedRepoError" or "gated" in text.lower() else \
                "auth" if "401" in text or "403" in text else "error"
            result = {"repo": repo, "error": kind, "type": name, "message": text.splitlines()[0][:500] if text else name}
        print(json.dumps(result), flush=True)
    return 1 if failed else 0


def check(modules: list[str]) -> int:
    import importlib
    from importlib.metadata import PackageNotFoundError, version

    found = {"python": sys.version.split()[0]}
    for name in modules:
        module = importlib.import_module(name)
        try:
            found[name] = version(name.replace("_", "-"))
        except PackageNotFoundError:
            found[name] = getattr(module, "__version__", "present")
        if name == "torch":
            found["cuda"] = bool(module.cuda.is_available())
    print(json.dumps(found), flush=True)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "fetch":
        return fetch(argv[1])
    if len(argv) >= 2 and argv[0] == "check":
        return check(argv[1:])
    print("usage: hf_fetch.py fetch SPEC_JSON | check MODULE...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
