# Knowledge reminder hooks (opt-in)

`claude-settings.hooks.json` is a hooks fragment in the schema shared by
Claude Code (`~/.claude/settings.json`) and Codex CLI (`~/.codex/hooks.json`).
It wires three small reminders; each is one `waterfree` command that reads the
event JSON on stdin and prints either nothing or a short `additionalContext`.

| Event | Command | What the agent sees |
|-------|---------|---------------------|
| `SessionStart` | `knowledge hook-session` | One line: how many shared lessons exist and how many were filed from this project, plus up to three of this project's most-retrieved entries. Silent when the store is empty. |
| `UserPromptSubmit` | `knowledge hook-context` | Up to three summary rows, only when an entry contains every key term of the prompt (or its identifiers). Silent otherwise. Reads `user_prompt` (Claude) or `prompt` (Codex). |
| `Stop` | `knowledge hook-stop` | When the final message says the fix took several attempts, needed a workaround, or had a non-obvious root cause, one `block` with a reason asking for the insight to be filed with `knowledge add` (or a one-line "not worth keeping"). Guarded by `stop_hook_active`, so it fires at most once per turn, and skipped when the message already mentions filing. |

Install user-wide (idempotent; re-running never duplicates a hook):

```powershell
.\skills\install_claude.ps1 -Skill waterfree-knowledge -InstallHooks   # -> ~/.claude/settings.json
.\skills\install_codex.ps1  -Skill waterfree-knowledge -InstallHooks   # -> ~/.codex/hooks.json
```

**Deploy the backend first.** The hooks call the `waterfree` on PATH. An older
installed build that does not know these actions exits with a usage error, and
a non-zero exit on `UserPromptSubmit` blocks the prompt in Claude Code. Check
with `waterfree knowledge hook-session < NUL` (PowerShell: `'{}' | waterfree
knowledge hook-session`) before installing.

Uninstall: remove the entries whose command starts with `waterfree knowledge hook-`.

Every firing is logged by the usage log (`knowledge hook-session`,
`hook-context`, `hook-stop`), so `waterfree usage summary --area knowledge`
shows how often each reminder fires and whether injected entries are then read
with `knowledge get`.
