"""Pinned native engines and model weight sets.

Everything here is pinned to an exact release asset or Hugging Face commit and
carries its SHA-256, so `install` is reproducible and a partial or tampered
download is caught instead of loaded. Bumping a version means editing this file
(and re-measuring ``vram_gb``), never a silent upstream change.

``vram_gb`` is the free VRAM a job needs before it starts: measured peak use
above the desktop's own on an RTX 3060 12 GB under Windows (2026-11-17, an
nvidia-smi sampler at 300-500 ms), plus up to ~0.5 GB of headroom where the
card has it. The preflight refuses to start below it, because on Windows an
over-budget job spills into system RAM and crawls instead of failing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from backend.app_paths import engines_dir, models_dir


@dataclass(frozen=True)
class RemoteFile:
    url: str
    name: str        # destination path, relative to the install folder
    size: int
    sha256: str


@dataclass(frozen=True)
class EngineRelease:
    key: str
    project: str
    version: str
    archive: RemoteFile
    executables: dict
    licence: str
    env: dict = field(default_factory=dict)   # extra environment for the engine process

    def install_dir(self) -> Path:
        return engines_dir() / self.key / self.version

    def executable(self, role: str) -> Path:
        return self.install_dir() / self.executables[role]


@dataclass(frozen=True)
class ModelSet:
    key: str
    label: str
    engine: str
    repo: str
    revision: str
    files: tuple
    licence: str
    vram_gb: float
    notes: str

    def install_dir(self) -> Path:
        return models_dir() / self.key

    def path(self, name: str) -> Path:
        return self.install_dir() / name

    @property
    def size_bytes(self) -> int:
        return sum(f.size for f in self.files)


def _hf(repo: str, revision: str, path: str, size: int, sha256: str, *, name: str | None = None) -> RemoteFile:
    return RemoteFile(
        url=f"https://huggingface.co/{repo}/resolve/{revision}/{path}",
        name=name or Path(path).name,
        size=size,
        sha256=sha256,
    )


def _gh(project: str, version: str, asset: str, size: int, sha256: str) -> RemoteFile:
    return RemoteFile(
        url=f"https://github.com/{project}/releases/download/{version}/{asset}",
        name=asset,
        size=size,
        sha256=sha256,
    )


# ---------------------------------------------------------------------------
# Engines (Windows x64, CUDA 13 builds; the RTX 30xx/40xx/50xx driver path)
# ---------------------------------------------------------------------------

ENGINES: dict[str, EngineRelease] = {
    "trellis": EngineRelease(
        key="trellis",
        project="pwilkin/trellis.cpp",
        version="v0.8.1",
        archive=_gh("pwilkin/trellis.cpp", "v0.8.1", "trellis-cuda-windows-x64.zip", 715040905,
                    "d2ea7b57174d3bff438064faf0b5fbcc55c29c31e6bba5f41030936fa366ed48"),
        executables={"server": "trellis-server.exe", "cli": "trellis-cli.exe"},
        licence="MIT",
    ),
    "openmoss": EngineRelease(
        key="openmoss",
        project="pwilkin/openmoss",
        version="v0.3.0",
        archive=_gh("pwilkin/openmoss", "v0.3.0", "moss-tts-cuda-windows-x64.zip", 649596656,
                    "def68a7f35e6bd111ec4118f4e88835547f3af87efd5f3812ab1e505d1f38229"),
        # The Windows release ships only the server (no moss-tts-cli).
        executables={"server": "moss-tts-server.exe"},
        licence="Apache-2.0",
    ),
    # The same openmoss release on ggml's Vulkan backend, used for
    # MOSS-SoundEffect only. Measured on an RTX 3060 (2026-11-17), one 3 s clip:
    # CUDA solve 72 s + DAC-VAE decode 577 s = 652 s; Vulkan solve 161 s +
    # decode 8.7 s = 174 s. The CUDA build's VAE decode is ~66x slower (an op
    # without a fast CUDA kernel, by the look of it), while its TTS codec decode
    # is fine (0.17 s a line) -- so speech and voice design stay on CUDA.
    "openmoss-vulkan": EngineRelease(
        key="openmoss-vulkan",
        project="pwilkin/openmoss",
        version="v0.3.0",
        archive=_gh("pwilkin/openmoss", "v0.3.0", "moss-tts-vulkan-windows-x64.zip", 15902158,
                    "709c5c67e76c4180a38c6516cf908665fa2df6613900298a7d2c58eba5914733"),
        executables={"server": "moss-tts-server.exe"},
        licence="Apache-2.0",
        # Vulkan accumulates matmuls in f16 by default (~1e-2 relative error on
        # this model, per openmoss docs/STATUS.md); this forces f32 at no
        # measurable cost.
        env={"GGML_VK_DISABLE_F16": "1"},
    ),
}


# ---------------------------------------------------------------------------
# Model weight sets
# ---------------------------------------------------------------------------

_TRELLIS_REPO, _TRELLIS_REV = "ilintar/trellis2-gguf", "a57397bd3d351599d9729fc144b3f87c3f87d65b"
_LOCAL_REPO, _LOCAL_REV = "ilintar/moss-tts-local-gguf", "9bfc4d52c9b2e8ee61c12384cbe606212bc743f7"
_SFX_REPO, _SFX_REV = "ilintar/moss-soundeffect-gguf", "b5556f201d6287d8cf1a18452d24f285903b82b8"
_VOICE_REPO, _VOICE_REV = "ilintar/moss-voicegen-gguf", "61c255dc85fcb4204bf6a21c23c191b52400344a"

MODEL_SETS: dict[str, ModelSet] = {
    "trellis2-q8": ModelSet(
        key="trellis2-q8",
        label="TRELLIS.2-4B (Q8_0) + DINOv3 + BiRefNet",
        engine="trellis",
        repo=_TRELLIS_REPO,
        revision=_TRELLIS_REV,
        files=tuple(_hf(_TRELLIS_REPO, _TRELLIS_REV, f"q8/{name}", size, sha) for name, size, sha in (
            ("birefnet.gguf", 882749024, "10c5dd4dcac904cf81c9a16180eb66f167dd52ab55867f54a44567b0b2babbc1"),
            ("dinov3.gguf", 323657920, "0dd4ffd4b46a248f5b7d49c35275d68461fbf73f57ddb4c1fa8afb4f7bb45a0d"),
            ("shape_dec.gguf", 881361568, "0de7c7a675022dd8696d526a9279e5a50b2a35c8a79452f434a72dc53d40f169"),
            ("shape_flow_1024.gguf", 1376125952, "997e9fc10ab95fda11c4cd1cbaf425101980ac36a2ef80e9c83e0b9c4bfc9680"),
            ("shape_flow_512.gguf", 1376125952, "29b639f4ff22ded8f91b619376a835f64b9874b0ebcadb9ac6c305195bf5d1f9"),
            ("ss_dec.gguf", 147379392, "2790b5eecb261cc877d9bf175ce2bd6dd48cd65be8c042c5f5bc023dfca01cf7"),
            ("ss_flow.gguf", 1376059040, "ea6d8a42b20661a5c6a52e5ffbdc1df7aca9b212792193873b418c69f871422c"),
            ("tex_dec.gguf", 881344576, "88b4fced46455e02f316664d5c43584a311921dd9a1cdc1b7b7d981cca9214d4"),
            ("tex_flow_1024.gguf", 1376178176, "cb2cb3aee74ba09c018f918ed8c146bc7e4f96335b61fa0d1fc1ff1a7811e6da"),
            ("tex_flow_512.gguf", 1376178176, "389a2cbdda59d53b21e5989650d9d36b7ac603266eaef06712cd07a9fc377210"),
        )),
        # DINOv3 is Meta's own licence, not MIT -- surfaced in every sidecar.
        licence="TRELLIS.2: MIT; DINOv3 ViT-L: Meta DINOv3 License; BiRefNet: MIT",
        # Measured: trellis-server loads each stage's weights on demand, so the
        # peak is per resolution (see meshgen.generate.VRAM_GB_BY_RESOLUTION).
        # This is the res-1024 default: 4.5 GB peak + headroom.
        vram_gb=5.0,
        notes="Image to UV-textured GLB with PBR textures.",
    ),
    "moss-voicegen": ModelSet(
        key="moss-voicegen",
        label="MOSS-VoiceGenerator 1.7B (BF16)",
        engine="openmoss",
        repo=_VOICE_REPO,
        revision=_VOICE_REV,
        files=(
            _hf(_VOICE_REPO, _VOICE_REV, "moss-voicegen.gguf", 4100169056,
                "b5637c2e362940ce7680e298c36c9b600193f50396e5bb2f3a0fc73c00fcbe92"),
            _hf(_VOICE_REPO, _VOICE_REV, "moss-voicegen.extras.gguf", 3687943616,
                "e7b3c2964d105e1b8348219f4c840ec2710865c33b2d251edefeb9920f300873"),
        ),
        licence="Apache-2.0",
        vram_gb=8.9,   # measured peak 8.4 GB at n_ctx 4096 (8.8 GB at the engine's 8192)
        notes="Designs a voice from a text description. 24 kHz mono.",
    ),
    "moss-tts-local-q8": ModelSet(
        key="moss-tts-local-q8",
        label="MOSS-TTS-Local-Transformer v1.5 (Q8_0)",
        engine="openmoss",
        repo=_LOCAL_REPO,
        revision=_LOCAL_REV,
        files=(
            _hf(_LOCAL_REPO, _LOCAL_REV, "moss-tts-local-1.5-q8_0.gguf", 4645051104,
                "71d19fe18e443749b72cdb1ab0575797676017393d37eaef6898eb791bba4069"),
            _hf(_LOCAL_REPO, _LOCAL_REV, "moss-tts-local-1.5-q8_0.extras.gguf", 4462667968,
                "687fec93e8e3a2bcc4c973444cbec41f88387bd1f297678dde093324e03090e6"),
        ),
        licence="Apache-2.0",
        # Measured peak 10.2 GB at n_ctx 4096 -- nearly the whole card once the
        # Windows desktop takes its ~1.5 GB, so the headroom here is thin on
        # purpose: at n_ctx 8192 (+0.6 GB KV) it no longer fit, spilled into
        # system RAM, and each line ran ~13x slower.
        vram_gb=10.3,
        notes="Speaks lines in a cloned voice. 48 kHz stereo.",
    ),
    "moss-sfx": ModelSet(
        key="moss-sfx",
        label="MOSS-SoundEffect v2.0 (BF16)",
        engine="openmoss-vulkan",   # see ENGINES["openmoss-vulkan"]: ~3.7x faster here
        repo=_SFX_REPO,
        revision=_SFX_REV,
        files=(
            _hf(_SFX_REPO, _SFX_REV, "moss-soundeffect-2.0.gguf", 4069678880,
                "676d3ead6b12e99ecff0a3c29172f0f3571b17834724b19559c4f8572135a8de"),
            _hf(_SFX_REPO, _SFX_REV, "moss-soundeffect-2.0.extras.gguf", 2985201184,
                "f905b2bb3be6e21908778e3d8ef8b0d06dc419cab1d3bb7613a29dc21d924b64"),
        ),
        licence="Apache-2.0",
        vram_gb=9.3,   # measured peak 8.8 GB on the Vulkan build
        notes="Text to sound effect, up to 30 s. 48 kHz mono.",
    ),
}
