# Seed RecallDeck Ops Deck (runbooks + NorthStar)

This repo keeps short operational references for board agents under `runbooks/`.

## What to seed
Import these files into the dedicated `RecallDeck` deck:
- `runbooks/orchestrator-playbook.md`
- `runbooks/board-orchestrator-northstar-summary.md`
- `runbooks/recalldeck-deck-scope.md`

Recommended tags:
- `ops/runbook`
- `clawd`

## How to seed (using RecallDeck tools)
1) Resolve deck ids:
   - Call `list_decks` and find the `RecallDeck` and `Docs` deck ids.
2) Import the files:
   - Call `import_file` for each file with `deck_id=<RecallDeck deck id>` and the tags above.

## Quick verification
- Call `recall` with `deck_ids=[<RecallDeck deck id>, <Docs deck id>]` and query: `orchestrator playbook` / `NorthStar`.
- The tool call arguments in logs should show `deck_ids` for recall/search calls and `deck_id` for imports/writes.
