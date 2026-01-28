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
- Key concepts mentioned in NorthStar:
  - scopes/tenants + strict RLS
  - hybrid retrieval (BM25 + ANN + optional reranking)
  - fast response targets (<100ms P50)
  - ingestion pipeline that produces cards + embeddings

## Evaluation (pending deeper review)
- Rook should evaluate whether RecallDeck can serve as an external memory backend for Clawdbot (vs file-based memory-core).
- Likely integration paths:
  - REST API client plugin (Clawdbot memory plugin)
  - MCP server integration

