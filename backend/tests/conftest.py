"""pytest bootstrap: keep test-driven calls out of the user's real global stores.

- usage log: off.
- knowledge store: a throwaway database for the whole session. Without this,
  any test that reaches `KnowledgeStore()` with no path (the CLI dispatcher,
  the tool registry) opened ~/.waterfree/global/knowledge.db and ran schema
  migrations on it. Subprocesses spawned by tests inherit the variable.
- GPU scheduler: off, with %LOCALAPPDATA%\\WaterFree redirected (see below).
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

# GPU scheduling: off by default, and every machine-wide root (GPU lease state,
# installed engines, model weights, engine logs) redirected to a throwaway
# folder. With the scheduler on, an imagegen/meshgen test would queue behind a
# real job, unload the developer's real Ollama models and read the real card's
# free VRAM. Tests of the scheduler itself turn it back on against their own dir.
os.environ["WATERFREE_GPU_SCHEDULER"] = "0"
if not os.environ.get("WATERFREE_LOCAL_DIR"):
    _local_dir = tempfile.mkdtemp(prefix="waterfree-test-local-")
    os.environ["WATERFREE_LOCAL_DIR"] = _local_dir
    atexit.register(shutil.rmtree, _local_dir, ignore_errors=True)
for _override in ("WATERFREE_GPU_DIR", "WATERFREE_ENGINES_DIR", "WATERFREE_MODELS_DIR"):
    os.environ.pop(_override, None)
