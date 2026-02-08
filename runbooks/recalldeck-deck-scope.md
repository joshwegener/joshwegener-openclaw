# RecallDeck Deck Scope (clawd / OpenClaw workers)

Goal: workers should recall/search primarily from:
- the dedicated `RecallDeck` deck (ops runbooks, NorthStar summaries, durable workflows)
- the system `Docs` deck (read-only library docs)

## Default scope
As of 2026-02-08:
- `RecallDeck` deck id: `2e134680-3970-4d8d-ad18-52220117ea5b`
- `Docs` deck id: `ee83fcd2-d6d2-4961-8572-a4d9c99a16cf`

Default retrieval scope (recall/search):
- `deck_ids = '["<RecallDeck deck id>","<Docs deck id>"]'` (tool expects a JSON array string)

Default write deck:
- `deck_id = RecallDeck` (never write to `Docs`)

If deck ids ever change (decks recreated), resolve by name using `list_decks` and update these ids.

## Audit / verification
When a worker uses RecallDeck tools, the tool call arguments in the worker log should show:
- `deck_ids` present for recall/search calls (when scope wasn't explicitly requested by the user/task)
- `deck_id` set to the `RecallDeck` deck for any imports/writes

