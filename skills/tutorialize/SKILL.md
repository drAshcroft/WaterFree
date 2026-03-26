---
name: tutorialize
description: Use the WaterFree tutorializer to generate developer tutorials for a downloaded repo using a local Ollama model, then store them in the knowledge base for agents and humans to read.
---

# Tutorialize a Repository

Generate structured developer tutorials for any repository using a **local Ollama LLM** and
store the results in the WaterFree knowledge base under `tutorial/{repo}/{focus}/{area}`.

Tutorials cover key architectural areas of the repo and are immediately searchable via
`search_knowledge` and `browse_knowledge_index`.

---

## When to Use

- A developer has cloned an unfamiliar repo and wants an accelerated onboarding
- You need to document how a dependency or third-party library is structured
- A user asks "walk me through how X works" for a repo that isn't already in the knowledge store

---

## Running the Tutorializer

```bash
python -m backend.tutorializer <path/to/repo>
```

The CLI will:
1. Connect to Ollama and let you pick a model (or auto-select if only one is installed)
2. Ask what you're most interested in learning
3. Analyse the repo structure (README, file tree, manifests)
4. Generate Markdown tutorials for the most relevant areas
5. Store each tutorial in the knowledge base

### Options

| Flag | Description |
|------|-------------|
| `--model llama3.2` | Skip model selection and use this model |
| `--focus "auth and permissions"` | Skip the interactive focus prompt |
| `--areas "Auth,Router,Models"` | Only generate tutorials for these named areas |
| `--base http://localhost:11434` | Ollama URL (default) |
| `--timeout 240` | Seconds to wait per LLM call (default 180) |
| `--list-models` | List available Ollama models and exit |

### Examples

```bash
# General overview of a Django project
python -m backend.tutorializer ~/repos/django-blog

# Focused on the API layer, using a specific model
python -m backend.tutorializer ~/repos/fastapi-app --model mistral --focus "API routing and middleware"

# Only generate tutorials for two specific areas
python -m backend.tutorializer ~/repos/my-app --areas "Authentication,Database Models"

# Non-interactive (for scripting)
python -m backend.tutorializer ~/repos/my-app --model llama3.2 --focus "data pipeline"
```

---

## Reading the Generated Tutorials

After running, tutorials are stored under `tutorial/{repo_name}/...` in the hierarchy.

```
# Browse all tutorials for a repo
browse_knowledge_index(path="tutorial/my-app", depth=2, include_entries=True)

# Search for a specific topic
search_knowledge(query="tutorial my-app authentication")

# List all repos that have been tutorialized
list_knowledge_sources()
```

---

## Tutorial Structure

Each generated tutorial is a Markdown document with these sections:

- **Overview** — What the area does and why it exists in the project
- **Key Concepts** — Core ideas to understand before reading the code
- **Code Walkthrough** — Step-by-step explanation referencing actual source files
- **Patterns & Conventions** — Recurring patterns and design decisions
- **Next Steps** — Related areas to explore and prerequisites

Tutorials are stored as `snippet_type = "tutorial"` with tags including `tutorial`,
the repo name slug, and the area name slug, making them easy to filter.

---

## Prerequisites

- Ollama must be running locally: `ollama serve`
- At least one model must be pulled: `ollama pull llama3.2`
- The WaterFree backend must be importable (run from the WaterFree project root)

---

## Notes

- Content is deduplicated by SHA-256 hash — re-running is safe and only adds new entries
- Larger models produce richer tutorials but take longer per area
- Use `--timeout` if a model is slow (codellama, larger llama variants)
- The `--focus` flag produces more targeted tutorials; omitting it generates a broad overview
