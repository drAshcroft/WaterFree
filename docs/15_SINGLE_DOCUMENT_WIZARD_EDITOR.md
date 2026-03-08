# Single-Document Wizard Editor

## Purpose

Build a single live editor for the `Bring Idea to Life` flow that feels like a real writing surface, not a markdown file and not a web page.

The editor must:
- present only the current chunk
- allow direct freeform editing
- support inline LLM clarification questions
- advance sequentially as each chunk is resolved
- maintain one growing permanent markdown document as the historical artifact

This replaces the current approach where workflow metadata, chunk markers, and machine state leak into the visible editing surface.

## Product Goals

The editor should:
- feel like a focused writing tool
- help the user start the project correctly
- keep the document human-readable at every stage
- let the AI ask structured follow-up questions without turning the experience into chat
- avoid showing future sections before they are relevant
- produce a permanent markdown record that grows over time

## Non-Goals

This editor is not:
- a markdown editor
- a rich-text office-style document editor
- a multi-pane workflow dashboard
- a collapsible outline viewer
- a chat UI
- a system that opens a new document for every stage

## Core UX Principles

### Editor, not page

The UI should look like a writing editor with a document surface, not like a settings page, card layout, or website.

### Only the current chunk exists

Only one chunk is visible at a time. Future chunks do not appear in the live editor until the current chunk is resolved.

### Human-first text

Visible content should read like a person authoring a document. Machine metadata must never be shown in the main editing surface.

### Temporary clarification UI

Clarification questions appear inline beneath the active chunk only when needed. Once answered, they disappear.

### One continuous artifact

The user stays in one editor experience while the permanent markdown artifact grows behind the scenes.

## Information Architecture

There are two representations of the same workflow:

### Canonical live state

Stored in an internal location, for example:
- `.waterfree/wizards/<run-id>/document.json`

This is the source of truth for the workflow.

### Permanent historical artifact

Stored in:
- `docs/<generated-name>.md`

This is regenerated from canonical state and acts as the readable long-term record.

The markdown file is not the live editing surface.

## Workflow Model

The workflow is chunk-based and sequential.

### Chunk progression

At any given time:
- exactly one chunk is active
- only the active chunk is visible/editable
- future chunks are hidden entirely
- previously resolved chunks are preserved in the exported markdown artifact

### Chunk lifecycle

Each chunk moves through these states:
- `draft`
- `awaiting_clarification`
- `resolved`
- `accepted`

Optional internal state:
- `submitted`
- `llm_processing`
- `error`

### Resolution rule

A chunk is resolved when:
- the user has provided enough content
- any required clarification questions have been answered
- the system determines the chunk can advance

Only after resolution may the next chunk be created.

## Editor UI Specification

### Main layout

Single-column editor layout with:
- fixed chunk title header
- optional short guidance text
- primary editable body area
- inline clarification block when needed
- action row at the bottom

No sidebar is required inside the editor itself.

### Header

The header is fixed and contains:
- current chunk title
- optional stage label in subtle text
- optional minimal status indicator

The header must not include technical IDs or workflow metadata.

### Editable body

The body is the main focus.

Requirements:
- large writing area
- direct in-place editing
- plain text or lightly structured text
- smooth autosave
- no markdown source formatting noise
- no visible chunk markers
- no generated YAML or comments

Behavior:
- user can type freely
- AI may rewrite or refine the text, but the result should still read like human-authored document prose
- body should remain highly editable after AI updates unless explicitly accepted

### Clarification block

Shown only when the active chunk needs clarification.

Placement:
- directly below the editable body

Contents:
- one or more clarification questions
- multiple-choice options for each question
- optional free-text override
- apply control

Behavior:
- questions are scoped only to the active chunk
- questions disappear once answered and incorporated
- questions should feel like editorial prompts, not chat messages
- previous clarification prompts are not permanently shown after resolution

### Action controls

Visible actions for the active chunk:
- `Submit`
- `Refine`

Optional later actions:
- `Accept`
- `Back`

Definitions:
- `Submit`: process the current chunk and decide whether clarification is needed or whether it can resolve
- `Refine`: ask the LLM to improve or sharpen the current text without advancing
- `Accept`: hard-lock a resolved chunk if explicit approval semantics remain necessary

For the initial implementation, `Submit` and `Refine` are sufficient.

## Visual Design Requirements

The visual treatment should feel like an editor.

Requirements:
- centered writing column
- generous whitespace
- restrained chrome
- calm typography
- clear cursor and focus behavior
- subtle status indications only
- no dashboard cards or workflow boxes unless absolutely necessary

Avoid:
- visible JSON-like UI
- obvious form labels everywhere
- heavy borders around every region
- web page hero layout
- over-styled button clusters

## Interaction Design

### Opening the editor

When the user launches the wizard:
- create or resume the canonical state
- open the webview editor
- show only the first unresolved chunk

### Typing

User edits the active chunk directly.
Changes autosave to canonical state.

### Submitting

When the user clicks `Submit`:
- save the current body
- send current chunk plus context to backend
- backend decides:
  - resolved with updated content, or
  - clarification needed

### Clarification

If clarification is needed:
- keep the same chunk open
- render inline questions beneath the body
- let user answer via multiple choice or free text
- resubmit clarification response
- update body and resolve or continue clarification

### Resolution and advance

When the chunk is resolved:
- append or refresh the markdown export
- mark current chunk resolved in canonical state
- instantiate the next chunk
- replace the editor header and body with the next chunk in the same document view

No new document opens.

## Content Rules

### Visible content

Visible content should include only:
- chunk title
- guidance text
- editable prose
- temporary clarification prompts
- user-facing buttons

### Hidden content

Hidden internal content includes:
- run IDs
- wizard IDs
- stage IDs
- chunk IDs
- workflow status codes
- system prompts
- machine notes
- exported metadata
- reconciliation data

These must never be shown in the editor body.

## Data Model

Suggested canonical state shape:

```json
{
  "runId": "string",
  "wizardId": "bring_idea_to_life",
  "title": "Bring Idea to Life",
  "currentStageId": "market_research",
  "currentChunkIndex": 0,
  "chunks": [
    {
      "id": "initial_goal",
      "title": "What is the idea?",
      "guidance": "Describe the software idea, problem you want to solve or frustration in plain language.",
      "body": "user-editable text",
      "status": "draft",
      "clarifications": [],
      "history": []
    }
  ],
  "export": {
    "markdownPath": "docs/market-research-xxxx.md",
    "lastExportedAt": "timestamp"
  },
  "createdAt": "timestamp",
  "updatedAt": "timestamp"
}
```

This shape is illustrative, not mandatory.

## Markdown Export Rules

The markdown export is the permanent human-readable historical artifact.

Requirements:
- append or regenerate deterministically from canonical state
- contain resolved chunk content in order
- remain readable as a normal document
- exclude machine metadata and internal workflow markers
- preserve the human-writing feel

## Clarification Rules

Clarification questions should be generated only when necessary.

Requirements:
- default to multiple choice when possible
- allow optional free-text override
- appear only under the active chunk
- disappear after use
- be stored in canonical state while active
- not pollute the permanent markdown export unless explicitly represented as resolved notes

## Sequential Stage Handling

The system should not go deep into the process visually before the user is ready.

Rules:
- do not show future chunks
- do not pre-render later stages
- do not open a new document when moving to the next chunk or stage
- keep the user in one continuous editor surface
- carry prior resolved content forward in state and export only

## Error Handling

If the backend fails:
- keep the user’s current text intact
- show a small inline error state
- allow retry
- do not lose clarification answers
- do not corrupt markdown export

## Autosave and Sync

Requirements:
- autosave body edits after a short debounce
- autosave clarification selections immediately
- regenerate markdown export after meaningful state changes
- opening the editor should always restore the current unresolved chunk

## Extension Integration

The webview editor should integrate with the existing extension flow.

Needed capabilities:
- `openWizard`
- `loadWizardState`
- `saveWizardDraft`
- `submitWizardChunk`
- `refineWizardChunk`
- `answerWizardClarification`
- `exportWizardMarkdown`

The current markdown-driven CodeLens flow should no longer be the primary editing mechanism for this wizard.

## MVP Scope

First implementation should include:
- webview editor shell
- one active chunk only
- editable body
- `Submit` and `Refine` buttons
- inline clarification area
- canonical JSON state
- markdown export to `docs`
- sequential advance to next chunk in the same editor

## Explicit Rejections

Do not implement:
- visible frontmatter
- visible `wf:*` markers
- collapsible future chunks
- multi-document step hopping
- chat transcript UI
- metadata panels in the writing surface
- separate markdown file as the editing source of truth

## Acceptance Criteria

This feature is correct when:
1. Launching the wizard opens a webview that feels like a writing editor.
2. The first visible content is only the current chunk, with no metadata noise.
3. The user can freely edit the chunk body.
4. Clicking `Submit` either advances the chunk or shows inline clarification questions.
5. Clarification questions support multiple-choice answers and disappear after resolution.
6. Future chunks are not visible before the current chunk is resolved.
7. The same editor view continues through the workflow; no new document is opened for each chunk.
8. A clean markdown artifact is saved in `docs` and grows over time.
9. Internal workflow state is stored separately and never shown in the editor body.
10. The result reads like a shared human-authored document that grows correctly from the start.
