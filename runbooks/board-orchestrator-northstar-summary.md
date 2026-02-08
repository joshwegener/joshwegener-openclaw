# clawd Board Orchestrator NorthStar (Ops Summary)

Purpose: a fast mental model for how RecallDeck’s Kanboard automation works, and what invariants to preserve when debugging.

## Core principles
- Kanboard is the source of truth; the orchestrator is a deterministic synchronizer.
- Active work must be observable: no silent WIP/Review without a recorded run or an explicit reason tag.
- File signals > log scraping. Logs are for humans, not control flow.
- When unsure: tag + comment + stop (avoid thrash).
- Critical work supersedes throughput.

## Canonical board model
- Project: `RecallDeck`
- Columns (titles must match): `Backlog`, `Ready`, `Work in progress`, `Review`, `Documentation`, `Blocked`, `Done`

## Control surface (tags + description hints)
- Hold / no automation:
  - `hold`, `no-auto`, `review:skip` (orchestrator must not move/start these)
  - Exception: `hold:queued-critical` is orchestrator-owned fencing for non-active criticals
- Repo mapping (first match wins):
  1) Tag `repo:<key>`
  2) Description `Repo: /absolute/path` or `Repo: <key>`
  3) (Optional/legacy) title hint behind `BOARD_ORCHESTRATOR_ALLOW_TITLE_REPO_HINT`
- Exemption: `no-repo` (meta/planning tasks)
- Dependencies: Description `Depends on: #31, #32` (also accepts `Dependencies:` / `Dependency:`)
- Exclusivity: Tag `exclusive:<key>` and/or description `Exclusive: key1, key2`
- Critical: tag `critical`

## Automation pause/blocked semantics (durable reason tags)
- Paused tags:
  - `paused` + reason tags like `paused:missing-worker`, `paused:critical`, `paused:thrash`
- Blocked reason tags (used to avoid filling Blocked column):
  - `blocked:repo`, `blocked:deps`, `blocked:exclusive`, `blocked:thrash`, `blocked:artifact`
- Review tags:
  - `review:auto`, `review:pending`, `review:inflight`, `review:pass`, `review:rework`, `review:error`, `review:retry`
- Docs tags:
  - `docs:auto`, `docs:pending`, `docs:inflight`, `docs:completed`, `docs:skip`, `docs:error`, `docs:retry`

## Run model (the core invariant)
Everything active is a run with a unique `runId` and a dedicated directory.

Run roots:
- Worker runs: `/Users/joshwegener/clawd/runs/worker/task-<id>/<runId>/`
- Review runs: `/Users/joshwegener/clawd/runs/review/task-<id>/<runId>/`
- Docs runs: `/Users/joshwegener/clawd/runs/docs/task-<id>/<runId>/`

Worker run artifacts (must exist for "done"):
- `worker.log` (human debug)
- `patch.patch` (non-empty)
- `kanboard-comment.md` (non-empty)
- `meta.json` (spawn metadata)
- `done.json` (canonical completion signal)

Reviewer run artifacts:
- `review.log` (human debug)
- `meta.json`
- `review.json` (canonical completion signal)

Docs run artifacts:
- `docs.log` (human debug)
- `meta.json`
- `patch.patch` (may be empty if docs not needed; file should still exist)
- `kanboard-comment.md`
- `done.json`

Control flow rule:
- Never infer completion from a stale file in an old run dir. Only accept completion via the run paths recorded in state.

## Worker spawn contract (stdout)
Spawn scripts must print one JSON object like:
- `execSessionId`
- `runId`, `runDir`
- `logPath`, `patchPath`, `commentPath`, `donePath`
- `startedAtMs`

## Fast troubleshooting checklist
1) Is there an active critical? If yes, ensure it’s actually able to start (repo mapping, deps, exclusives).
2) Any WIP tasks missing a recorded worker handle? If yes, start/record or pause deterministically.
3) Any Ready/Backlog tasks auto-tagged `blocked:*` that can be unblocked by simple edits (Repo/Depends/Exclusive tags)?
4) "Done but not advanced": patch+comment exist but card didn’t move. Move to Review and file a root-cause ticket.

