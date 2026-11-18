"""The Kimodo engine: a pinned Python venv (:mod:`backend.engines.pyenv`) plus HF-cache weights.

* Its own venv: Kimodo pins ``transformers==5.1.0`` (its LLM2Vec code is only
  tested there) while imagegen runs a newer one.
* Python 3.12, because PyAV (a Kimodo dependency) ships Windows wheels only for
  cp312 and later.
* ``kimodo`` builds a small C++ extension (MotionCorrection, its foot-skate
  clean-up) with CMake, so the install needs a C++ compiler: MSVC Build Tools
  on Windows. ``cmake`` itself comes from PyPI into the venv, installed first.

Weights stay in the Hugging Face cache, like imagegen's (the user's choice):
the gated Meta-Llama-3-8B-Instruct base (~15 GB), the two LLM2Vec adapters and
the Kimodo checkpoint (~1.1 GB). The worker runs offline.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from backend.app_paths import local_app_dir
from backend.engines import pyenv
from backend.engines.pyenv import HfRepo, PythonEngine

INSTALL_HINT = "waterfree animgen install"
WORKER = Path(__file__).with_name("kimodo_worker.py")
MODEL = "kimodo-soma-rp-v1.2"      # Kimodo's own key for the pinned checkpoint
CHECKPOINT_REPO = "nvidia/Kimodo-SOMA-RP-v1.2"

Report = Callable[[str], None]

_LLAMA = "meta-llama/Meta-Llama-3-8B-Instruct"
_MNTP = "McGill-NLP/LLM2Vec-Meta-Llama-3-8B-Instruct-mntp"
_SUPERVISED = "McGill-NLP/LLM2Vec-Meta-Llama-3-8B-Instruct-mntp-supervised"
_COMMIT = "0c967d388652f7c70ed26ee4bbedc674e378d522"   # tag v1.2
_LLAMA_FILES = (
    "config.json", "generation_config.json", "model.safetensors.index.json",
    "model-00001-of-00004.safetensors", "model-00002-of-00004.safetensors",
    "model-00003-of-00004.safetensors", "model-00004-of-00004.safetensors",
    "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json",
)

KIMODO = PythonEngine(
    key="kimodo",
    project="nv-tlabs/kimodo",
    version="v1.2",
    commit=_COMMIT,
    python="3.12",
    torch="torch==2.12.1",
    torch_index="https://download.pytorch.org/whl/cu126",
    stages=(("cmake>=4.2",), (f"kimodo @ git+https://github.com/nv-tlabs/kimodo.git@{_COMMIT}",)),
    check_imports=("torch", "kimodo", "motion_correction"),
    needs_compiler=True,
    repos=(
        HfRepo(CHECKPOINT_REPO, "0e3c89024d896064b6851201373f7834784a386b",
               required=("config.yaml", "model.safetensors"),
               licence="NVIDIA Open Model License (commercial use allowed)", size_gb=1.1),
        HfRepo(_MNTP, "31474e395ada192e8ed1586db6be79fb3b70c9c0",
               required=("adapter_config.json", "adapter_model.safetensors", "config.json", "tokenizer.json"),
               licence="MIT", size_gb=0.2),
        HfRepo(_SUPERVISED, "baa8ebf04a1c2500e61288e7dad65e8ae42601a7",
               required=("adapter_config.json", "adapter_model.safetensors"), licence="MIT", size_gb=0.2),
        # The full repo also holds original/consolidated.00.pth, a second 16 GB
        # copy of the weights that transformers never reads -- hence the file list.
        HfRepo(_LLAMA, "8afb486c1db24fe5011ec46dfbe5b5dccdb575c2", files=_LLAMA_FILES, required=_LLAMA_FILES,
               gated=True, licence="Meta Llama 3 Community License", size_gb=15.0),
    ),
    licence=("Kimodo code: Apache-2.0; Kimodo-SOMA-RP-v1.2 weights: NVIDIA Open Model License; "
             "text encoder: Meta Llama 3 Community License (Meta-Llama-3-8B-Instruct) + LLM2Vec adapters (MIT)"),
    venv_gb=5.0,
)


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def read_marker() -> dict:
    return pyenv.read_marker(KIMODO)


def status() -> dict:
    return pyenv.status(KIMODO)


def require() -> dict:
    return pyenv.require(KIMODO, install_hint=INSTALL_HINT)


def install(*, report: Report = _stderr) -> dict:
    return pyenv.install(KIMODO, report=report)


def encoder_id(marker: dict) -> str:
    """Identity of the text encoder, part of every cached embedding's key."""
    revisions = marker.get("revisions", {})
    parts = [f"{repo}@{revisions.get(repo, '?')}" for repo in (_LLAMA, _MNTP, _SUPERVISED)]
    return "llm2vec-bf16-cpu-batch1:" + "+".join(parts)


def embedding_cache_dir() -> Path:
    return local_app_dir() / "cache" / "animgen-text-embeddings"


def worker_env() -> dict:
    return pyenv.offline_env({
        "TEXT_ENCODER_DEVICE": "cpu",       # the user's choice: the 8B encoder never goes on the GPU
        "TEXT_ENCODER_MODE": "local",       # don't probe for a text-encoder server on :9550
        "LOCAL_CACHE": "true",              # Kimodo: resolve its checkpoint from the cache first
    })


def worker_argv(marker: dict) -> list[str]:
    return [str(KIMODO.python_exe()), "-I", str(WORKER), "serve", "--model", MODEL,
            "--cache-dir", str(embedding_cache_dir()), "--encoder-id", encoder_id(marker)]
