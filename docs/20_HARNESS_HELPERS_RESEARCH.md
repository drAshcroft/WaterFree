# 20 — Harness helpers: what the evidence says, what our usage log says

Written 2026-09-25. Two inputs: a literature and vendor-documentation survey of
retrieval, memory and telemetry for coding agents (2023–2026), and a backfill of
WaterFree's own CLI usage from 145 Claude Code transcripts on this machine
(2026-08-24 → 2026-09-25) via `waterfree usage import-transcripts`.

The question behind both: the graph index feels useful, the knowledge base feels
less useful. Is that true, and what should change?

---

## 1. What we measured (transcript backfill, 2,318 paired calls, 123 sessions)

| Area | Calls | Share | Note |
|------|------:|------:|------|
| todos | 1,665 | 72% | update 493, list 403, add 295, search 295 |
| knowledge | 369 | 16% | add 221, search 115, browse 21 |
| testing | 265 | 11% | run-one 153, run 62, logs 46 |
| index | 7 | 0.3% | never called by agents in other projects |
| vision / qa-summary / qa | 12 | 0.5% | |

Knowledge, specifically:

- **1.9 adds per search.** Agents write to the store far more than they read it.
- **102 of 1,306 entries (7.8%) have ever been retrieved** by a search, and the
  most-retrieved entry came back three times. The store is a write-mostly diary.
- Search costs **2.6 KB median, 9.7 KB at p90**, 465 KB total over the month, and
  6% of searches returned nothing. Compare todos search: 700 B median.
- The store is global: 20% of entries are asset-catalog rows, top source repo is
  `c:/projects/itch_assets`, and 114 entries have no taxonomy path. Sample
  queries showed one common word ("column", "add") pulling unrelated projects'
  entries above the exact match under the old any-term ranking.

Todos, specifically: `todos list` spent 1.18 MB of context in a month (median
975 B, p90 8.2 KB), and 23% of `todos search` calls returned nothing under the
old phrase-only matching. Both are addressed by the 2026-09-25 CLI fixes
(term matching, summary rows, true totals).

The graph index: the "graphify feels useful" impression cannot come from agent
CLI calls; there were seven in a month. It must come from the extension's
dashboard or direct use. Instrumenting that path is a follow-up (see §4).

## 2. What the literature says (summary; sources in §5)

1. **Agentic grep/glob/read beats one-shot index lookups on SWE-bench-scale
   repos, and low-precision retrieval actively hurts.** Anthropic dropped RAG in
   early Claude Code (Cherny, 2025). SWE-Explore (2026): BM25 region hit-rate
   0.065 vs 0.50+ for agentic explorers. CodeGrep (2026): BM25 *degraded* an
   OpenHands agent (+38.6% tokens recovering from false positives); only a
   high-precision tool-based retriever helped. Better Call Grep (ISSTA 2026):
   naive lexical retrieval matches graph-based baselines on CrossCodeEval.
2. **Embeddings earn their keep only at scale; hybrid is the honest answer.**
   Cursor's A/B (2025): +12.5% answer accuracy with semantic search on top of
   grep, concentrated in 1,000+ file repos. Agent Retrieval Bench (2026): no
   single retrieval family dominates; RepoMap-style structural context won at
   an 8K-token budget.
3. **Graph/repo-map context has practitioner adoption but thin controlled
   evidence.** Aider publishes no ablation. The one independent signal is the
   budgeted-yield result above: structure wins when the token budget is small.
   Graphify's "71.5x fewer tokens" is a user claim, not a study.
4. **Memory helps only when curated.** SWE Context Bench (2026): accurately
   summarized prior experience improves resolution and cuts cost; unfiltered or
   wrongly selected context is neutral or negative. Memory Transfer Learning
   (2026): abstract insights transfer (+3.7% avg); low-level traces cause
   negative transfer. Evaluating AGENTS.md (2026): repository overviews were
   not useful and raised cost >20%; instructions were followed.
5. **Consolidation is now a first-class primitive.** Anthropic's Dreams and
   Claude Code's Auto Dream merge duplicates, resolve contradictions to the
   latest value, and rewrite relative dates; Letta's sleep-time compute does
   the same offline. Motivation: observed drift filling the memory budget.
6. **Injection timing beats availability.** The API memory tool and Managed
   Agents inject a mandatory "check memory first" line; Graphify installs a
   PreToolUse hook; Claude Code hooks can return `additionalContext` at the
   moment of need. Pull-based search only works if the agent thinks to call it.
7. **Telemetry:** Claude Code exports OTel metrics/events (`tool_result` with
   `tool_name`, `duration_ms`, `result_tokens`, joined on `prompt.id`) and hook
   events; there is no standard "was the retrieved context used" metric. Best
   proxies: first-useful-hit and context-efficiency (SWE-Explore, r≈0.93–0.95
   with repair success), precision thresholds (CodeGrep).
8. **Anthropic on tool suites:** CLI tools are "the most context-efficient way
   to interact with external services"; return few high-signal fields, not
   UUIDs; paginate/truncate; treat tool descriptions as prompts and iterate with
   evals; skills for sometimes-relevant procedures, CLAUDE.md for always-true
   facts, hooks for must-always-happen actions.

## 3. Where our data and the literature agree

- **Precision over recall, small payloads.** Our 9.7 KB p90 knowledge search
  and 8.2 KB p90 todos list are exactly the low-precision, high-cost shape the
  studies penalise. Done today: summary rows by default, `get <id>` for bodies,
  all-terms-first ranking with term-coverage re-ranking, own-project entries
  preferred, `--repo` filter.
- **The knowledge base is a diary, not a memory.** 92% of entries never come
  back. The literature's answer is not "search harder" but curate: abstract
  insights over traces, consolidation passes, and retrieval pushed at the right
  moment rather than waiting for a `knowledge search` call.
- **The graph index's evidence-backed niche is budgeted structure**
  (callers/callees, impact, cycles), not symbol search. Its CLI is not reached
  by agents today; the extension path is where its value is felt and where it
  should be measured.

## 4. Recommendations (filed as todos; ranked)

Status 2026-09-25 (same day): items 1–6 below are implemented. Extension
requests and prompt injection are logged (USAGE-001); `knowledge consolidate`
exists and was applied once (KB-001: 3 merges, 6 date rewrites; 620 entries
listed as never retrieved in 90 days); the store is scoped global / project /
assets with a one-time backfill (KB-002: 766 / 284 / 262); the prompt hook is
shipped opt-in (KB-003, not installed by default); `index` actions take a token
budget and `architecture` defaults to a small aspect set (IDX-001); `knowledge
add` warns on near-duplicates, missing paths and trace-like text (KB-004). The
research itself is in the knowledge base under `harness-design/`.


1. **Measure before more building.** The usage log is live from this commit:
   every CLI call records area, action, query, hits, returned bytes, agent and
   session. Re-run `waterfree usage summary --since 30d` in a month and compare
   against §1. Add the extension's graph dashboard and `serve` handlers to the
   same log so the index's real usage is visible.
2. **Knowledge consolidation pass ("dream").** A local-model job (Ollama is
   already wired for tutorialize/qa-summary) that clusters near-duplicate
   entries, merges them, rewrites relative dates to absolute, and demotes
   entries never retrieved in 90 days. Report, then apply with `knowledge
   update`/`delete`. Evidence: Dreams, Auto Dream, Memory Transfer Learning.
3. **Split the store by kind.** Asset-catalog rows (262) and per-project
   lessons should not share one BM25 index with cross-project conventions.
   Add a `scope` (global / project / assets) and default search to global +
   current project.
4. **Push, don't only pull.** A `UserPromptSubmit` hook that runs `knowledge
   search` on the prompt against the current project and injects at most three
   summary rows as `additionalContext` when all terms match. Gate on the
   precision threshold: no all-term match, no injection. Evidence: CodeGrep,
   SWE Context Bench, Managed Agents memory instructions.
5. **Index: budgeted answers.** Give `trace`, `detect-changes` and
   `god-nodes` a token budget (Aider repo-map style) and make `architecture`
   default to the smallest useful aspect set. Do not add embeddings until the
   log shows agents calling the index at all.
6. **Entry quality gate on `knowledge add`.** Warn when a description reads
   like a trace ("I ran X then Y") rather than an insight, when no
   hierarchy path is given, or when a near-duplicate title exists (all-terms
   search on the title before insert). Evidence: AGENTS.md study, MTL.

## 5. Sources

Anthropic: Effective context engineering for AI agents (Sep 2025);
Writing effective tools for agents (Sep 2025); Agent Skills (Oct 2025); Code
execution with MCP (Nov 2025); Effective harnesses for long-running agents
(Nov 2025); Claude Code docs — memory, best practices, hooks, monitoring
(OTel); Claude API memory tool; Managed Agents memory stores and Dreams.
Boris Cherny on agentic search vs RAG (X, 2025); Latent Space Claude Code
episode (May 2025).

Studies: SWE-Explore (arXiv 2606.07297); CodeGrep (arXiv 2608.05886); Better
Call Grep (arXiv 2601.23254, ISSTA 2026); CORE-Bench (arXiv 2606.11864);
Agent Retrieval Bench (arXiv 2607.24882); CodeRAG-Bench (NAACL Findings 2025);
Evaluating AGENTS.md (arXiv 2602.11988); Is Progressive Disclosure All You
Need (arXiv 2607.17598); SWE Context Bench (arXiv 2602.08316); Memory Transfer
Learning (arXiv 2604.14004); Harness the Memory (arXiv 2608.15008); The
Context Fails First (arXiv 2607.14275); Canary Tools (arXiv 2608.04719);
Generative Agents (arXiv 2304.03442); MemoryBank (arXiv 2305.10250); A-MEM
(arXiv 2502.12110); Zep/Graphiti (arXiv 2501.13956); Mem0 (arXiv 2504.19413).

Vendors: Cursor, Improving agent with semantic search (Nov 2025);
Sourcegraph, How Cody understands your codebase (Feb 2024) and arXiv
2408.05344; Aider repo map (Oct 2023, docs); Graphify (GitHub, 2026); Jason
Liu / Colin Flaherty on grep vs embeddings at Augment (Sep 2025); Letta
sleep-time compute (Apr 2025); Mem0 State of AI Agent Memory 2026.

Caveats: Cursor's and Mem0's numbers are self-reported; Augment's SWE-Bench
Pro claim has no public retrieval ablation; Zep's paper is Zep's own. Several
2026 arXiv preprints are not yet peer-reviewed.
