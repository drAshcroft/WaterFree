"""Audio QA engine for `waterfree audiogen ... --qa` -- runs in the audioqa venv, not WaterFree's.

    python -I audioqa_worker.py serve --host H --port P

POST /qa {"items": [{"id", "wav", "kind": "speech"|"sfx", "text"?, "prompt"?, "language"?}]}
-> {"results": [{"id", "transcript"?, "language"?, "clapScore"?, "error"?}]}

* speech -- faster-whisper large-v3 transcribes the take; WaterFree compares
  the transcript with the requested words.
* sfx    -- LAION CLAP embeds the prompt and the audio; the cosine similarity
  says how well they match (only meaningful relative to other takes of the
  same prompt, so WaterFree ranks variants by it).

Each model loads on first use, so a speech-only batch never loads CLAP.
Standard library only at import.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

WHISPER_REPO = "Systran/faster-whisper-large-v3"
CLAP_REPO = "laion/clap-htsat-fused"
LOG_PREFIX = "audioqa:"


def log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", flush=True)


class Engine:
    def __init__(self) -> None:
        # torch first: its wheel ships the cuBLAS 12 / cuDNN 9 DLLs that ctranslate2
        # (faster-whisper) needs on Windows, and importing it puts them on the DLL path.
        import torch

        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self._whisper = None
        self._clap = None

    def whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel

            started = time.monotonic()
            compute = "float16" if self.device == "cuda" else "int8"
            self._whisper = WhisperModel(WHISPER_REPO, device=self.device, compute_type=compute)
            log(f"Whisper large-v3 loaded on {self.device} in {time.monotonic() - started:.0f}s")
        return self._whisper

    def clap(self):
        if self._clap is None:
            from transformers import ClapModel, ClapProcessor

            started = time.monotonic()
            model = ClapModel.from_pretrained(CLAP_REPO).to(self.device).eval()
            self._clap = (model, ClapProcessor.from_pretrained(CLAP_REPO))
            log(f"CLAP loaded on {self.device} in {time.monotonic() - started:.0f}s")
        return self._clap

    def speech(self, item: dict) -> dict:
        segments, info = self.whisper().transcribe(
            item["wav"], language=item.get("language") or None, beam_size=5,
            condition_on_previous_text=False, vad_filter=False)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return {"transcript": text, "language": info.language,
                "languageProbability": round(float(info.language_probability), 3)}

    def sfx(self, item: dict) -> dict:
        from faster_whisper.audio import decode_audio

        torch = self.torch
        model, processor = self.clap()
        audio = decode_audio(item["wav"], sampling_rate=48000)          # mono float32, resampled by PyAV
        with torch.no_grad():
            text_in = processor(text=[item["prompt"]], return_tensors="pt", padding=True).to(self.device)
            audio_in = processor(audio=[audio], sampling_rate=48000, return_tensors="pt").to(self.device)
            text_vec = _features(model.get_text_features(**text_in))
            audio_vec = _features(model.get_audio_features(**audio_in))
        text_vec = text_vec / text_vec.norm(dim=-1, keepdim=True)
        audio_vec = audio_vec / audio_vec.norm(dim=-1, keepdim=True)
        return {"clapScore": round(float((text_vec * audio_vec).sum()), 4)}

    def run(self, items: list[dict]) -> list[dict]:
        results = []
        for index, item in enumerate(items, start=1):
            started = time.monotonic()
            try:
                answer = self.speech(item) if item["kind"] == "speech" else self.sfx(item)
            except Exception as exc:  # noqa: BLE001 - one bad file must not sink the batch
                traceback.print_exc()
                answer = {"error": f"{type(exc).__name__}: {exc}"}
            log(f"[{index}/{len(items)}] {item['kind']} checked in {time.monotonic() - started:.1f}s")
            results.append({"id": item["id"], **answer})
        return results


def _features(output):
    """transformers 5 may return a model output instead of a tensor from get_*_features."""
    if hasattr(output, "pooler_output") and output.pooler_output is not None:
        return output.pooler_output
    if hasattr(output, "norm"):
        return output
    return output[0]


def serve(args: argparse.Namespace) -> int:
    engine = Engine()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._reply(200, {"ok": True, "device": engine.device})
            else:
                self._reply(404, {"error": f"unknown path {self.path}"})

        def do_POST(self) -> None:
            if self.path != "/qa":
                self._reply(404, {"error": f"unknown path {self.path}"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                items = json.loads(self.rfile.read(length) or b"{}").get("items") or []
            except ValueError as exc:
                self._reply(400, {"error": f"bad request: {exc}"})
                return
            self._reply(200, {"results": engine.run(items)})

    server = HTTPServer((args.host, args.port), Handler)
    log(f"listening on {args.host}:{args.port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, required=True)
    return serve(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
