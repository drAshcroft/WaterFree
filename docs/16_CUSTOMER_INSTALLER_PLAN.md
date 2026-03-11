# Subsystem 16 — Customer Installer And MCP Registration
## WaterFree VS Code Extension

---

## Purpose

WaterFree needs a real customer installer for Claude Code and Codex MCP integration.

The current `deploy.ps1` flow is a developer convenience script. It is not a reliable
customer installation mechanism because it depends on repo-local paths, direct config
mutation, ambient Python availability, and weak verification.

This document defines the target installer, the migration plan, and the release gates
required before WaterFree can be distributed as a professional product.

---

## Problem Statement

The current deployment flow fails for structural reasons:

- it assumes the product is being run from a source checkout
- it assumes `python` is present and resolvable from `PATH`
- it assumes PowerShell launch behavior is stable across customer machines
- it edits Claude/Codex config files directly instead of using their supported MCP CLIs
- it treats "config written" as equivalent to "server installed and launchable"
- it does not produce deterministic install logs, rollback, or repair behavior

That is acceptable for local prototyping. It is not acceptable for customer deployment.

---

## External Constraints

### Official Claude Code MCP surface

Claude Code exposes MCP management through its CLI:

- `claude mcp add`
- `claude mcp get`
- `claude mcp list`
- `claude mcp remove`

Claude also supports explicit scopes such as `user` and `project`.

### Official Codex MCP surface

Codex exposes MCP management through its CLI:

- `codex mcp add`
- `codex mcp get`
- `codex mcp list`
- `codex mcp remove`

Codex MCP configuration is currently stored under `~/.codex/config.toml`, but the
installer must not treat that file format as the primary integration contract. The CLI
surface is the supported contract.

### Design rule

WaterFree must register MCP servers through official product commands first and use
direct config inspection only for diagnostics.

---

## Installer Principles

1. No source checkout required.
2. No user-managed Python prerequisite for MCP launch.
3. No direct config-file editing as the primary install path.
4. Re-running the installer must be safe and idempotent.
5. Install success must mean "registered and launchable", not merely "files copied".
6. All actions must produce supportable logs and clear error states.
7. Upgrade, repair, and uninstall must be first-class paths.

---

## Target User Experience

Customer flow:

1. Run `WaterFreeSetup.exe`
2. Choose optional integration targets:
   - VS Code extension
   - Claude Code MCP servers
   - Codex MCP servers
3. Installer performs preflight checks and shows exact status for each target.
4. Installer copies the WaterFree runtime into a stable local application directory.
5. Installer registers WaterFree MCP servers through `claude mcp add` and `codex mcp add`.
6. Installer verifies each registration through `list` or `get`, then performs a launch smoke test.
7. Installer writes an install summary and support log.

Support flow:

- `Repair` re-copies runtime assets, removes stale WaterFree registrations, re-registers,
  and re-verifies.
- `Uninstall` removes WaterFree MCP registrations and installed files.

---

## Recommended Packaging

### Recommendation

Ship a Windows installer based on WiX Toolset.

Recommended shape:

- MSI for the installed runtime and product files
- bootstrapper EXE if extra prerequisites or chained packages are needed later

Why:

- standard enterprise Windows installation model
- upgrade and uninstall semantics are well understood
- supports logging, product codes, repair, rollback, and code signing
- better fit for customer support than a raw PowerShell script

### Non-goal

The first production installer does not need to support macOS or Linux. It needs to work
reliably on supported Windows versions before platform expansion is considered.

---

## Runtime Packaging Strategy

### Current problem

Today the MCP servers are launched via:

`powershell -> start_mcp_server.ps1 -> python -m backend.mcp_*`

That creates several failure points:

- PowerShell availability and policy
- external Python version mismatch
- missing Python packages
- repo-relative script paths

### Target runtime

Install WaterFree under a stable path such as:

`%LocalAppData%\WaterFree\`

Recommended runtime layout:

```text
%LocalAppData%\WaterFree\
  bin\
    waterfree-mcp.exe
  runtime\
    python\
    backend\
    assets\
  logs\
  install\
    manifest.json
```

### Launcher contract

The installer should register a stable launcher command, not a repo script.

Example:

```text
waterfree-mcp.exe index
waterfree-mcp.exe knowledge
waterfree-mcp.exe todos
waterfree-mcp.exe debug
waterfree-mcp.exe testing
```

### Packaging options

Option A:
- bundle a private embedded Python runtime plus the backend package

Option B:
- freeze each MCP server into a standalone executable

Recommendation:
- start with one private embedded Python runtime plus one stable launcher

Reason:
- lower packaging complexity than freezing multiple binaries
- easier to patch backend code without maintaining five separate executables

---

## Registration Strategy

### Core rule

Do not hand-edit Claude or Codex config files during normal installation.

### Claude registration

Use:

```text
claude mcp add --scope user waterfree-index -- "<installed launcher>" index
claude mcp add --scope user waterfree-knowledge -- "<installed launcher>" knowledge
claude mcp add --scope user waterfree-todos -- "<installed launcher>" todos
claude mcp add --scope user waterfree-debug -- "<installed launcher>" debug
claude mcp add --scope user waterfree-testing -- "<installed launcher>" testing
```

### Codex registration

Use:

```text
codex mcp add waterfree-index -- "<installed launcher>" index
codex mcp add waterfree-knowledge -- "<installed launcher>" knowledge
codex mcp add waterfree-todos -- "<installed launcher>" todos
codex mcp add waterfree-debug -- "<installed launcher>" debug
codex mcp add waterfree-testing -- "<installed launcher>" testing
```

### Migration cleanup

Before adding new servers:

- remove stale `pairprogram-*` registrations if present
- remove stale `waterfree-*` registrations that point at old paths

Use `remove` through each product CLI rather than deleting config sections manually.

---

## Verification Strategy

Install is not complete until all three layers succeed.

### Layer 1 — Runtime verification

- launcher binary exists
- launcher can start each MCP server process
- runtime dependencies are present

### Layer 2 — Registration verification

- `claude mcp get <name>` succeeds for Claude targets
- `codex mcp get <name>` succeeds for Codex targets
- returned command matches installed WaterFree path

### Layer 3 — Behavioral smoke verification

- start each registered MCP server once
- verify process exits or waits in expected MCP mode without immediate startup failure
- write success or failure per server to install log

---

## Preflight Checks

The installer must check and report:

- supported Windows version
- writable install directory
- whether VS Code is installed
- whether Claude Code CLI is installed and callable
- whether Codex CLI is installed and callable
- whether existing WaterFree installation is present
- whether stale Pairprogram registrations exist

Possible outcomes per target:

- `installed`
- `already current`
- `repaired`
- `skipped not installed`
- `failed`

---

## Logging And Diagnostics

Write logs to a stable support path such as:

`%LocalAppData%\WaterFree\logs\installer\`

Required artifacts:

- timestamped installer log
- machine-readable result manifest
- per-target registration status
- launcher smoke-test results
- detected versions of `claude` and `codex`

Support requirement:

Customer support must be able to ask for one folder and reconstruct the install outcome.

---

## Upgrade, Repair, And Uninstall

### Upgrade

- detect installed WaterFree version
- replace runtime files atomically where possible
- remove old WaterFree MCP registrations
- add fresh registrations using current launcher path
- re-run verification

### Repair

- re-copy runtime
- re-register MCP servers
- re-run smoke tests
- preserve user data and logs

### Uninstall

- remove all WaterFree MCP registrations from selected targets
- delete installed runtime files
- leave user-created workspace `.waterfree/` data untouched unless explicitly requested

---

## Workstreams

### Workstream 1 — Installer architecture

Deliverables:

- WiX project
- install layout
- versioning and upgrade strategy
- log and manifest format

### Workstream 2 — Runtime packaging

Deliverables:

- embedded Python or equivalent private runtime
- stable `waterfree-mcp` launcher
- packaged backend assets

### Workstream 3 — MCP registration engine

Deliverables:

- Claude registration wrapper
- Codex registration wrapper
- stale registration cleanup
- registration verification

### Workstream 4 — Test harness

Deliverables:

- clean-machine test script
- smoke tests for each server
- upgrade, repair, and uninstall tests

### Workstream 5 — Release operations

Deliverables:

- installer signing
- release notes
- support playbook
- known issue policy

---

## Execution Phases

### Phase 1 — Stop the bleeding

Goals:

- declare `deploy.ps1` developer-only
- stop using direct config mutation as the intended customer path
- document current failure modes and the new installer direction

### Phase 2 — Build the private runtime

Goals:

- package backend into an installable local runtime
- replace repo-relative MCP launch paths with installed paths

### Phase 3 — Build CLI-driven registration

Goals:

- install through `claude mcp ...` and `codex mcp ...`
- remove stale `pairprogram-*`
- verify each registration deterministically

### Phase 4 — Build the Windows installer

Goals:

- MSI or bootstrapper
- install, upgrade, repair, uninstall flows
- proper logs and error dialogs

### Phase 5 — Release qualification

Goals:

- clean-VM test matrix passes
- signed installer
- support documentation complete

---

## Acceptance Criteria

The installer is release-ready only if all of the following pass on a clean Windows VM:

- user can install WaterFree without a source checkout
- user does not need a preinstalled Python runtime for MCP launch
- Claude integration succeeds when Claude is installed
- Codex integration succeeds when Codex is installed
- each WaterFree MCP server is visible through product CLI inspection after install
- each server launches from the installed runtime path, not the repo path
- re-running install is idempotent
- repair restores broken registrations
- uninstall removes WaterFree registrations cleanly
- stale `pairprogram-*` registrations do not survive migration

---

## Test Matrix

Minimum matrix:

- Windows 11 clean machine, Claude only
- Windows 11 clean machine, Codex only
- Windows 11 clean machine, Claude and Codex
- machine with stale `pairprogram-*` registrations
- machine with stale WaterFree path registrations
- upgrade from prior WaterFree installer version
- repair after manual config damage
- uninstall after successful install

Stretch matrix:

- Windows 10 supported baseline
- non-admin install
- restrictive PowerShell policy
- PATH missing Python

---

## Immediate Next Steps

1. Mark `deploy.ps1` as dev-only in the docs and README.
2. Create a dedicated installer project under the repo, separate from extension build logic.
3. Implement a prototype `waterfree-mcp` installed launcher.
4. Implement CLI-driven registration wrappers for Claude and Codex.
5. Stand up a clean-VM smoke test workflow before further customer distribution.

---

## References

- Anthropic Claude Code MCP documentation: https://docs.anthropic.com/en/docs/claude-code/mcp
- OpenAI Codex MCP documentation: https://developers.openai.com/codex/mcp/
