---
name: waterfree-writing-grade
description: Grade fiction, poetry, scripts, and other creative writing from a local file with six calibrated 0-20 craft scores and concise feedback. Use when the user asks to grade, rate, score, judge, or evaluate creative writing; do not use for ordinary proofreading or technical-document review unless numeric creative-writing scores are requested.
---

# WaterFree — Writing Grade

Run the genre-aware grader through the WaterFree CLI. It returns JSON on stdout.

```bash
waterfree writing-grade grade <file> --workspace <project-root>
```

Use an absolute file path when the file is not inside the current working
directory. The file argument is resolved against the shell's current directory;
`--workspace` only selects `.waterfree/providers.json` and its model routing.

The grader scores these dimensions from 0 through 20:

- `creativity`
- `syntax_grammar_prose`
- `understandability`
- `interest`
- `structure_pacing_coherence`
- `voice_tone_emotional_impact`

Each dimension contains an integer `score` and exactly one short feedback
sentence. `overall.score` is computed by the CLI out of 120, along with an
overall percentage. Report the JSON result directly unless the user asks for a
different presentation; do not silently replace the tool's scores with your own.

Short files are graded directly. Long files are chunked, analyzed in order, and
reduced to a whole-work grade; inspect `evidence_mode` and `chunks_processed` if
the evaluation method matters to the user.

Grading can take 30 s to 3 minutes depending on the load

## Cost-aware routing

The command uses the opt-in `creative_writing` reader stage. For inexpensive
bulk grading, route an OpenRouter provider to that stage and set its model to
`auto:free`. This selects zero-priced models first, then a short cheapest-paid
fallback tail, then local Ollama if configured:

```json
{
  "models": {"creative_writing": "auto:free"},
  "routing": {"useForStages": ["creative_writing"]}
}
```

The standalone CLI reads the OpenRouter credential from
`OPENROUTER_API_KEY`; it cannot access VS Code SecretStorage. If no provider
claims the stage, the command uses local Ollama with
`freehuntx/qwen3-coder:14b` by default. Override that local model with
`WATERFREE_WRITING_GRADE_MODEL`.

Exit `2` means invalid or empty input, `3` means the file was not found, and
exit `4` means no configured model target was available.
