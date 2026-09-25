---
name: waterfree-usage
description: Use the `waterfree usage` CLI to see how the waterfree helpers (todos, knowledge, index, testing) are actually being called — volumes, empty-search rates, context cost per call, which knowledge entries get retrieved — when asked whether a helper is worth keeping, tuning, or documenting differently.
---

# WaterFree — Usage log

Every `waterfree <area> <action>` call appends one JSON record to
`~/.waterfree/global/usage.jsonl` (area, action, query, exit code, duration,
bytes written to stdout, the envelope's `total`/`returned`/`truncated`/`hint`,
the first ten returned ids, the calling agent and Claude Code session id).
The log is fail-silent and never changes a command's output or exit code.
`waterfree usage ...` itself is not logged.

Turn it off with `WATERFREE_USAGE_LOG=0`; redirect it with
`WATERFREE_USAGE_LOG=<path>`. Tests set it to `0` automatically.

## Commands

```bash
waterfree usage summary                       # last 30 days, every area
waterfree usage summary --since 7d --area knowledge
waterfree usage summary --since all --workspace C:/Projects/dungeon
waterfree usage summary --source transcript   # only backfilled history
waterfree usage tail -n 20                    # most recent records
waterfree usage path                          # where the files live
waterfree usage import-transcripts            # backfill from ~/.claude/projects
```

`summary` returns `records`, `sessions`, `by_area`, `by_agent`, `by_workspace`,
`by_day`, per-action rows (`calls`, `sessions`, `error_rate`,
`median_result_bytes`, `p90_result_bytes`, `total_result_bytes`,
`zero_hit_rate`, `median_hits`, `median_duration_ms`), a `knowledge` block
(`searches`, `adds`, `adds_per_search`, `distinct_entries_retrieved`,
`top_retrieved_entries`), `top_queries` and `top_empty_queries`.

`import-transcripts` reads Claude Code's local session transcripts, pairs each
`waterfree` shell call with its tool result, and rewrites
`usage-transcripts.jsonl` wholesale (safe to re-run). Those records carry
`"source": "transcript"`; live ones carry `"source": "cli"`.

## Reading the numbers

- A high `zero_hit_rate` on a search action means agents are asking in words
  the store does not use, or the store lacks the entry. Check
  `top_empty_queries` before changing the ranking.
- `total_result_bytes` is context spent. Compare it with `calls` to see which
  action is expensive per call, and prefer summary rows plus `get <id>`.
- `distinct_entries_retrieved` against `waterfree knowledge stats` says what
  share of the knowledge base ever comes back. Entries never retrieved are
  candidates for consolidation or deletion.
- `by_area` with `index` near zero means the graph is not being reached from
  agents' shells; its value, if any, is coming through the extension.

See `docs/20_HARNESS_HELPERS_RESEARCH.md` for the baseline measured on
2026-09-25 and the recommendations that depend on these numbers.
