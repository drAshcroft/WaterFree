"""Opt-in automatic QA for generated audio (``--qa``; user decision 2026-11-18, todo GPU-007).

* Speech and voice-design takes are transcribed with faster-whisper large-v3
  and compared with the requested text: word error rate, dropped words, added
  words, and stutters (a word said twice where the text has it once). Voice
  design candidates are ranked by it -- a candidate that misreads its sample
  line makes a poor reference voice.
* SFX takes are scored against their prompt with LAION CLAP and ranked within
  each prompt (the raw cosine is not calibrated across prompts).

The models run in their own venv (:mod:`backend.engines.pyenv`), installed by
``waterfree audiogen install qa``, as a second stage inside the same exclusive
GPU lease after openmoss has stopped.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path

from backend.engines import pyenv
from backend.engines.pyenv import HfRepo, PythonEngine
from backend.gpu.engine import EngineError, EngineServer

INSTALL_HINT = "waterfree audiogen install qa"
WORKER = Path(__file__).with_name("audioqa_worker.py")
REVIEW_WER = 0.15        # a take above this word error rate is flagged for a listen

AUDIOQA = PythonEngine(
    key="audioqa",
    project="SYSTRAN/faster-whisper + LAION CLAP",
    version="v1",
    python="3.12",
    torch="torch==2.12.1",
    torch_index="https://download.pytorch.org/whl/cu126",
    # The faster-whisper/ctranslate2/av trio is the one already proven on this
    # machine (C:\Projects\transcribe); transformers matches WaterFree's venv.
    # av is pinned because faster-whisper 1.2.1 passes av.open(metadata_errors=...),
    # which PyAV 19 removed: with the newest av every decode fails with a TypeError.
    stages=(("faster-whisper==1.2.1", "ctranslate2==4.8.1", "av==18.1.0", "transformers==5.12.1"),),
    check_imports=("torch", "faster_whisper", "ctranslate2", "transformers"),
    repos=(
        HfRepo("Systran/faster-whisper-large-v3", "edaa852ec7e145841d8ffdb056a99866b5f0a478",
               required=("model.bin", "config.json", "tokenizer.json", "vocabulary.json", "preprocessor_config.json"),
               licence="MIT", size_gb=2.9),
        HfRepo("laion/clap-htsat-fused", "365dea6ef167def6676140ed93bbc43f84dabb28",
               required=("config.json", "model.safetensors", "preprocessor_config.json", "tokenizer.json"),
               licence="Apache-2.0", size_gb=0.6),
    ),
    licence="faster-whisper: MIT; Whisper large-v3 (CTranslate2): MIT; LAION CLAP: Apache-2.0",
    venv_gb=4.5,
)

Report = Callable[[str], None]


def status() -> dict:
    return pyenv.status(AUDIOQA)


def require() -> dict:
    return pyenv.require(AUDIOQA, install_hint=INSTALL_HINT)


def install(*, report: Report) -> dict:
    return pyenv.install(AUDIOQA, report=report)


# ---------------------------------------------------------------------------
# Running a batch
# ---------------------------------------------------------------------------

def run(items: list[dict], *, report: Report) -> None:
    """Check every item; write ``item["entry"]["qa"]`` (and the sidecar). Never raises for a QA failure.

    Each item: ``{"entry": result dict with "audio"/"metadata", "kind": "speech"|"sfx",
    "text"|"prompt", "language"?, "group"?}``. ``group`` ties takes that are
    ranked against each other (design candidates, variants of one SFX prompt).
    """
    if not items:
        return
    report(f"QA: checking {len(items)} take(s) (Whisper for speech, CLAP for SFX)")
    payload = [{"id": str(k), "wav": item["entry"]["audio"], "kind": item["kind"],
                "text": item.get("text"), "prompt": item.get("prompt"), "language": item.get("language")}
               for k, item in enumerate(items)]
    try:
        with EngineServer(name="audioqa", argv=[str(AUDIOQA.python_exe()), "-I", str(WORKER), "serve"],
                          cwd=AUDIOQA.install_dir(), env=pyenv.offline_env(), report=report,
                          echo_log=True, startup_timeout=300) as server:
            body, _ = server.post_json("/qa", {"items": payload}, timeout=300 + 30 * len(items))
        results = {r["id"]: r for r in json.loads(body).get("results", [])}
    except (EngineError, ValueError) as exc:
        report(f"QA failed, the audio itself is fine: {str(exc).splitlines()[0]}")
        for item in items:
            _attach(item["entry"], {"error": str(exc).splitlines()[0]})
        return
    for k, item in enumerate(items):
        raw = results.get(str(k), {"error": "no result"})
        if raw.get("error"):
            verdict = {"error": raw["error"]}
        elif item["kind"] == "speech":
            verdict = {**score_speech(item.get("text") or "", raw.get("transcript", "")),
                       "language": raw.get("language")}
        else:
            verdict = {"clapScore": raw.get("clapScore")}
        item["verdict"] = verdict
    _rank(items)
    for item in items:
        _attach(item["entry"], item["verdict"])


def _rank(items: list[dict]) -> None:
    groups: dict[str, list[dict]] = {}
    for item in items:
        if item.get("group") is not None and "error" not in item["verdict"]:
            groups.setdefault(item["group"], []).append(item)
    for members in groups.values():
        if len(members) < 2:
            continue
        if members[0]["kind"] == "speech":
            # Real errors first; spelling variants only break ties.
            ordered = sorted(members, key=lambda m: (m["verdict"]["errors"] + len(m["verdict"]["repeated"]),
                                                     m["verdict"]["wer"]))
        else:
            ordered = sorted(members, key=lambda m: -(m["verdict"]["clapScore"] or 0))
        for rank, member in enumerate(ordered, start=1):
            member["verdict"]["rank"] = rank
            member["verdict"]["of"] = len(ordered)


def _attach(entry: dict, verdict: dict) -> None:
    entry["qa"] = verdict
    sidecar = Path(entry.get("metadata", ""))
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    data["qa"] = verdict
    sidecar.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def summary(files: list[dict]) -> dict | None:
    """Result-level digest: takes to listen to, and the best take per ranked group."""
    checked = [f for f in files if "qa" in f]
    if not checked:
        return None
    review = [f["audio"] for f in checked if f["qa"].get("review") or f["qa"].get("error")]
    best = [f["audio"] for f in checked if f["qa"].get("rank") == 1]
    return {"checked": len(checked), "review": review, "best": best}


# ---------------------------------------------------------------------------
# Speech scoring
# ---------------------------------------------------------------------------

def words(text: str) -> list[str]:
    """Lower-cased words, punctuation dropped (apostrophes inside words kept)."""
    text = unicodedata.normalize("NFKC", text).lower().replace("\u2019", "'")
    return re.findall(r"[\w]+(?:'[\w]+)*", text)


SPELLING_SIMILARITY = 0.8   # traveller/traveler 0.89, doubts/doubt 0.83; stew/stool 0.4, traveller/trudler 0.56


def _similarity(a: str, b: str) -> float:
    """1 - character edit distance / longer length."""
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return 1 - previous[-1] / max(1, len(a), len(b))


def score_speech(reference: str, transcript: str) -> dict:
    """How well ``transcript`` matches ``reference``: word error rate, and which errors are real.

    Whisper writes what it hears in its own spelling, so "traveller" comes back
    as "traveler": a substitution between near-identical words is reported as a
    spelling variant, not a misreading. A real misread word ("stew" heard as
    "stool"), a stutter, or a high error rate flags the take for a listen.
    (Measured 2026-11-18 on real openmoss takes: the one known misread
    candidate had a WER of only 0.105, under any sane overall threshold.)
    """
    scored = _align(reference, transcript)
    variants = [[ref, hyp] for ref, hyp in scored["substitutions"] if _similarity(ref, hyp) >= SPELLING_SIMILARITY]
    misread = [[ref, hyp] for ref, hyp in scored["substitutions"] if _similarity(ref, hyp) < SPELLING_SIMILARITY]
    hard = len(misread) + len(scored["dropped"]) + len(scored["added"])
    hard_wer = hard / max(1, scored["words"])
    return {
        "transcript": transcript,
        "wer": scored["wer"],
        "errors": hard,
        "misread": misread,
        "dropped": scored["dropped"],
        "added": scored["added"],
        "repeated": scored["repeated"],
        "spellingVariants": variants,
        "review": bool(misread) or bool(scored["repeated"]) or hard_wer > REVIEW_WER,
    }


def _align(reference: str, transcript: str) -> dict:
    """Word-level edit alignment: WER plus the substituted, dropped, added and repeated words."""
    ref, hyp = words(reference), words(transcript)
    rows, cols = len(ref) + 1, len(hyp) + 1
    cost = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        cost[i][0] = i
    for j in range(cols):
        cost[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            same = ref[i - 1] == hyp[j - 1]
            cost[i][j] = min(cost[i - 1][j - 1] + (0 if same else 1), cost[i - 1][j] + 1, cost[i][j - 1] + 1)
    substitutions, dropped, added = [], [], []
    i, j = len(ref), len(hyp)
    while i > 0 or j > 0:
        if i > 0 and j > 0 and cost[i][j] == cost[i - 1][j - 1] + (0 if ref[i - 1] == hyp[j - 1] else 1):
            if ref[i - 1] != hyp[j - 1]:
                substitutions.append((ref[i - 1], hyp[j - 1]))
            i, j = i - 1, j - 1
        elif i > 0 and cost[i][j] == cost[i - 1][j] + 1:
            dropped.append(ref[i - 1])
            i -= 1
        else:
            added.append(hyp[j - 1])
            j -= 1
    ref_pairs = {(ref[k], ref[k + 1]) for k in range(len(ref) - 1)}
    repeated = [hyp[k] for k in range(1, len(hyp)) if hyp[k] == hyp[k - 1] and (hyp[k], hyp[k]) not in ref_pairs]
    # A stutter shows up as an added word too; count it once, as a repeat.
    for word in repeated:
        if word in added:
            added.remove(word)
    return {
        "words": len(ref),
        "wer": round(cost[-1][-1] / max(1, len(ref)), 3),
        "substitutions": list(reversed(substitutions)),
        "dropped": list(reversed(dropped)),
        "added": list(reversed(added)),
        "repeated": repeated,
    }
