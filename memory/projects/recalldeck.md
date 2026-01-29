# RecallDeck (project)

## Location (local repos)
- Root: `/Users/joshwegener/Projects/RecallDeck/`
- Repos:
  - `RecallDeck-Server/`
  - `RecallDeck-Web/`
  - `RecallDeck-MCP/`
  - `RecallDeck-Docs/`
  - `RecallDeck-Deckhand/`

## North Star doc
- `RecallDeck-Server/northstar.md`

## Quick notes (Rook)
- RecallDeck is intended to be a **memory-first, multi-tenant semantic retrieval engine** built around time-aware, provenance-rich **atomic cards**.
- Current MVP focus: reliable **store** (`/v1/memories`) + **recall** (`/v1/recall`) loop for dogfooding.

## Current decisions (as of 2026-01-29)
- Decks: **de-scoped for MVP** (land clean later).
- Idempotency: `canonical_key` **returns existing** on duplicate.
- Deckhand: **on hold** (do not work on it).

## Model/workflow preference (Josh)
- Planning/analysis: GPT (high).
- Implementation: Codex (use `ghigh` for planning, `chigh` for coding).
- Edge-case review: Claude (high) when needed.

## Evaluation (pending deeper review)
- Rook should evaluate whether RecallDeck can serve as an external memory backend for Clawdbot (vs file-based memory-core).
- Likely integration paths:
  - REST API client plugin (Clawdbot memory plugin)
  - MCP server integration
