# WaterFree — Live Pair Debug
## Subsystem: Summon AI During Active Debug Session

---

## Purpose

Live Pair Debug allows the developer to summon the AI mid-debug, at a breakpoint, with **live program state** already loaded into context. Instead of describing a bug to the AI from memory, the AI sees exactly what the developer sees: the call stack, variable values, the code at the breakpoint, and any active exception.

This is the same WaterFree negotiation model — intent before action — but triggered on-demand from the debugger rather than from a planned task.

---

## Trigger

**Right-click menu inside the editor, while a debug session is paused:**

```
Live Pair Debug   (only visible when inDebugMode)
```

Also available via command palette: `WaterFree: Live Pair Debug`

---

## What the AI Receives

```
DEBUG CONTEXT
─────────────
File:   src/session/session_manager.py
Line:   87

Code at breakpoint (± 10 lines):
  84   def save_session(self, doc: PlanDocument) -> None:
  85       doc.updated_at = _now()
  86       self._sessions_dir.mkdir(parents=True, exist_ok=True)
→ 87       self._current_path.write_text(json.dumps(doc.to_dict(), indent=2))
  88

Call stack:
  save_session         session_manager.py:87
  handle_save_session  server.py:195
  dispatch             server.py:220

Variables — Local:
  self._current_path = PosixPath('/workspace/.waterfree/sessions/current.json')
  doc.id             = "8bc25e27-..."
  doc.goal_statement = "Build the PlanningPanel webview"

Exception (if any):
  PermissionError: [Errno 13] Permission denied: '/workspace/.waterfree/sessions/current.json'
```

---

## What the AI Returns

A `DebugAnalysis` with:

| Field | Content |
|---|---|
| `diagnosis` | What the variable values and stack indicate — plain English, specific |
| `likely_cause` | Root cause hypothesis based on visible state |
| `suggested_fix` | Minimum change to resolve the issue (as an IntentAnnotation) |
| `questions` | Things to verify before a fix can be confirmed |

The `suggested_fix` follows the same IntentAnnotation schema — the developer can Approve it to apply the edit, or Alter/Redirect as normal.

---

## Integration Points

- **VS Code Debug Adapter Protocol (DAP):** Extension tracks `stopped` events to capture `threadId`. Queries `stackTrace`, `scopes`, and `variables` via `session.customRequest()`.
- **Context limit:** Variables are capped at 100 entries, values truncated at 500 chars. Deeply nested objects are summarised as `{...}`.
- **No debug session:** If no session is active or paused, shows an info message and exits.
- **Exception handling:** If a debug adapter does not support variable queries (some don't), the capture degrades gracefully to file + line + stack only.

---

## Relationship to Active Session

If a WaterFree session is active:
- The debug analysis is added as a note to the current session
- The `suggested_fix` annotation is attached to the current task (or a new task is inserted)
- AI state transitions to `awaiting_review`

If no session is active:
- A lightweight one-off analysis is shown in a VS Code information panel
- The developer can choose to start a full session from the result

---

## Out of Scope

- Watching variables over time (run-to-breakpoint tracking)
- Modifying watch expressions or breakpoints
- Reading stdout/stderr from the debug console directly
