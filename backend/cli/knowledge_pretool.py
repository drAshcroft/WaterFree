"""PreToolUse hook: warn when a Bash call pipes a backslash-bearing edit through a heredoc.

The lesson "Bash-tool heredocs mangle backslashes in edit scripts" was filed a
dozen times from ten projects and kept being hit, because a prompt-matching
hook never sees it: prompts don't mention heredocs. This hook looks at the
tool call itself, at the moment of need (HOOK-HEREDOC-001).

It never blocks. On a match it prints PreToolUse `additionalContext` (the
model sees it; the call still runs under the normal permission flow); on
anything else, including malformed input, it prints nothing and exits 0.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass

# The canonical knowledge entry; cited when it still exists.
LESSON_ID = "202d702d-4705-4f08-8591-596342139d49"

# Claude Code's Bash tool; Codex's shell tools.
SHELL_TOOLS = frozenset({"Bash", "shell", "local_shell", "exec_command", "container.exec"})

_HEREDOC = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_INTERPRETER = re.compile(r"(?:^|[\s;&|(/\\])(?:python[0-9.]*|py|node|perl|ruby|php|deno|bun)(?:\.exe)?(?=\s|$)",
                          re.IGNORECASE)
_REDIRECT = re.compile(r"(?:^|\s)(?:>>?|\|\s*tee\b)\s*\S")
_EDIT = re.compile(r"write_text|write_bytes|writeFile|open\([^)\n]*['\"][wa+]|\.replace\(|re\.sub\(|"
                   r"\bsed\s+-i|>>?\s*\S", re.IGNORECASE)
_CODE_PATH = re.compile(
    r"[\w.\-/\\:]+\.(?:gd|gdshader|tscn|tres|py|pyi|ts|tsx|js|jsx|mjs|cjs|cs|cpp|cc|cxx|c|h|hpp|"
    r"rs|go|java|kt|lua|json|ya?ml|toml|md|ps1|sh)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Heredoc:
    header: str      # the line holding `<<`
    delimiter: str
    quoted: bool
    body: str


def command_text(tool_input: object) -> str:
    """The shell command from a Claude (`{"command": str}`) or Codex (`{"command": [argv]}`) payload."""
    if not isinstance(tool_input, dict):
        return ""
    command = tool_input.get("command")
    if isinstance(command, list):
        return "\n".join(str(part) for part in command)
    return command if isinstance(command, str) else ""


def heredocs(command: str) -> list[Heredoc]:
    """Every heredoc in `command` with its body (up to the delimiter line, or the end)."""
    found: list[Heredoc] = []
    lines = command.splitlines()
    i = 0
    while i < len(lines):
        matches = list(_HEREDOC.finditer(lines[i]))
        header_index = i
        i += 1
        for match in matches:  # several heredocs on one line are read in order
            strip_tabs = match.group(1) == "-"
            delimiter = match.group(3)
            body: list[str] = []
            while i < len(lines):
                line = lines[i]
                i += 1
                if (line.lstrip("\t") if strip_tabs else line).rstrip("\r") == delimiter:
                    break
                body.append(line)
            found.append(Heredoc(header=lines[header_index], delimiter=delimiter,
                                 quoted=bool(match.group(2)), body="\n".join(body)))
    return found


def risky_heredoc(command: str) -> Heredoc | None:
    """The first heredoc that edits a code file with a backslash in its body, else None.

    All three must hold: a backslash in the body; the heredoc feeds an
    interpreter, is redirected into a file, or its body edits one; and a code
    or config path appears in the command. A plain `cat <<EOF` of prose, or a
    script without backslashes, passes silently.
    """
    if "<<" not in command or "\\" not in command:
        return None
    if not _CODE_PATH.search(command):
        return None
    for doc in heredocs(command):
        if "\\" not in doc.body:
            continue
        if _INTERPRETER.search(doc.header) or _REDIRECT.search(doc.header) or _EDIT.search(doc.body):
            return doc
    return None


def warning_text(doc: Heredoc, *, lesson_title: str | None) -> str:
    kind = "unquoted heredoc" if not doc.quoted else "heredoc"
    cite = (f" Lesson: `waterfree knowledge get {LESSON_ID[:8]}` ({lesson_title})." if lesson_title else "")
    return (
        f"WaterFree warning: this Bash call sends an edit through a {kind} (<<{doc.delimiter}) whose "
        "body contains backslashes. Backslashes do not survive that path intact: a trailing "
        "backslash-newline (a line continuation) is joined away, and escapes such as \\n, \\\" and "
        "doubled Windows-path backslashes change, usually silently (an anchor stops matching, or the "
        "written file is corrupted but still parses). Prefer the Edit tool for this change; for a "
        "scripted edit, write the script with the Write tool and run it from a file, build "
        "backslashes with chr(92), and assert every anchor matched." + cite
    )


def evaluate(event: object, lesson_lookup=None) -> str | None:
    """additionalContext for a PreToolUse event, or None when nothing should be said."""
    if not isinstance(event, dict):
        return None
    if str(event.get("tool_name") or "") not in SHELL_TOOLS:
        return None
    doc = risky_heredoc(command_text(event.get("tool_input")))
    if doc is None:
        return None
    title = None
    if lesson_lookup is not None:
        try:
            title = lesson_lookup(LESSON_ID)
        except Exception:  # noqa: BLE001 - a store problem must not break the hook
            title = None
    return warning_text(doc, lesson_title=title)


def run(stdin=None, stdout=None, lesson_lookup=None) -> tuple[int, str | None]:
    """Read the event JSON, print the hook output on a match.

    Returns (exit code, emitted text). The exit code is always 0: for PreToolUse,
    exit 2 would block the tool call, and this hook must only ever advise.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        raw = stdin.read()
        event = json.loads(raw) if raw.strip() else None
    except (OSError, ValueError):
        return 0, None
    text = evaluate(event, lesson_lookup=lesson_lookup)
    if text:
        stdout.write(json.dumps({
            "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text},
        }) + "\n")
    return 0, text


def _lesson_title(entry_id: str) -> str | None:
    """Title of the cited entry, or None when it was merged away or the store is unavailable."""
    from backend.knowledge.store import KnowledgeStore  # noqa: PLC0415 - only on a match

    entry = KnowledgeStore().get_entry(entry_id)
    return entry.title if entry else None


def _log_firing(argv: list[str], duration_ms: float, text: str) -> None:
    """Usage-log the firings only; logging every Bash call would swamp the log."""
    try:
        from backend.cli import usage_log  # noqa: PLC0415

        usage_log.append(usage_log.build_record(
            area="knowledge", action="hook-pretool", argv=argv, workspace="",
            exit_code=0, duration_ms=duration_ms, result_bytes=len(text), payload=None,
        ))
    except Exception:  # noqa: BLE001 - logging must never break the hook
        pass


def main(argv: list[str] | None = None) -> int:
    """`waterfree knowledge hook-pretool` fast path (see backend/main.py).

    Runs before every Bash tool call, so it bypasses the CLI dispatcher, whose
    eager area imports cost ~0.7 s. Ignores its arguments (the hook config
    passes none; --workspace is tolerated).
    """
    import time  # noqa: PLC0415

    started = time.perf_counter()
    try:
        _, text = run(lesson_lookup=_lesson_title)
    except Exception:  # noqa: BLE001 - never fail, never block
        return 0
    if text:
        _log_firing(list(argv or []), (time.perf_counter() - started) * 1000.0, text)
    return 0
