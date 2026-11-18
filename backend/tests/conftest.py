"""pytest bootstrap: keep test-driven calls out of the user's real global stores.

- usage log: off.
- knowledge store: a throwaway database for the whole session. Without this,
  any test that reaches `KnowledgeStore()` with no path (the CLI dispatcher,
  the tool registry) opened ~/.waterfree/global/knowledge.db and ran schema
  migrations on it. Subprocesses spawned by tests inherit the variable.
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

os.environ.setdefault("WATERFREE_USAGE_LOG", "0")

if not os.environ.get("WATERFREE_KNOWLEDGE_DB"):
    _kb_dir = tempfile.mkdtemp(prefix="waterfree-test-kb-")
    os.environ["WATERFREE_KNOWLEDGE_DB"] = str(Path(_kb_dir) / "knowledge.db")
    atexit.register(shutil.rmtree, _kb_dir, ignore_errors=True)
