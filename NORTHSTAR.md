# RecallDeck Board Automation NorthStar (clawd)

This document is the end-to-end contract for how RecallDeck’s Kanboard, the orchestrator, Codex workers, and Claude reviewers work together.

Repo (“ops brain”):
- `/Users/joshwegener/clawd`

This NorthStar reflects the **run-id + file-signaled** design:
- Workers/reviewers run in **tmux windows** you can watch.
- Completion is **file-based** (`done.json`, `review.json`) instead of log scraping.
- The orchestrator behaves like a deterministic state machine (no WIP ↔ Review ping-pong).

---

## Scope And Principles

Principles:
- The board is the source of truth; the orchestrator is a deterministic synchronizer.
- No “silent” WIP or Review: if a card is active, there is an active run or a clear reason tag.
- File signals > log scraping. Logs are for humans, not control flow.
- When unsure: tag + comment + stop (avoid thrash).
- Critical work supersedes throughput.

Out of scope:
- RecallDeck product repos (examples under `/Users/joshwegener/Projects/RecallDeck/*`)
- Gateway plumbing (OpenClaw/Telegram). This repo assumes Kanboard is reachable.

---

## Configuration (Secrets Live Outside Git)

Secrets/config file (not committed):
- `/Users/joshwegener/.config/clawd/orchestrator.env`

Key invariants:
- The orchestrator process MUST have `KANBOARD_BASE`, `KANBOARD_USER`, `KANBOARD_TOKEN` in its environment.
- tmux/launchd often do not propagate `HOME`. We treat the env file path as explicit:
  - `CLAWD_ORCHESTRATOR_ENV_FILE` (preferred)
  - `CLAWD_ENV_FILE` (legacy alias)

Pass threshold:
- `BOARD_ORCHESTRATOR_REVIEW_THRESHOLD` (default `90`)

Leases:
- `BOARD_ORCHESTRATOR_USE_LEASES` is **off by default** (`0`) in the run-id world.

---

## Board Model (Canonical)

Project:
- Kanboard project name: `RecallDeck`

Columns (titles must match):
- `Backlog`
- `Ready`
- `Work in progress`
- `Review`
- `Blocked`
- `Done`

Swimlanes:
- Orchestrator uses a stable `swimlanePriority` list in state for ordering.

---

## Tags (Control Surface)

Never override:
- `hold` / `no-auto` / `review:skip` (the orchestrator must not move/start these)
- Exception: `hold:queued-critical` is orchestrator-owned. If a card is fenced by `hold:queued-critical`,
  the orchestrator may remove it when that card becomes the active critical. (Legacy cleanup: it may also remove
  a plain `hold` tag **only** when `hold:queued-critical` is present.)

Repo mapping (first match wins):
1) Tag: `repo:<key>`
2) Description line: `Repo: /absolute/path` or `Repo: <key>`
3) (Optional/legacy) Title hint behind `BOARD_ORCHESTRATOR_ALLOW_TITLE_REPO_HINT`

Exemptions:
- `no-repo`: meta/planning tasks; may still be reviewed

Dependencies:
- Description line: `Depends on: #31, #32` (also accepts `Dependencies:` / `Dependency:`)

Exclusivity:
- Tag: `exclusive:<key>` and/or description line `Exclusive: key1, key2`

Critical:
- `critical`
- Queuing: when multiple critical cards exist, non-active critical cards are fenced with `hold:queued-critical` so they don't start.
- Active: the active critical is automatically unfenced and allowed to start.

Pause semantics:
- `paused` (generic)
- Reason tags (preferred when automation pauses):
  - `paused:missing-worker`
  - `paused:critical`
  - `paused:thrash`

Blocked reason tags (automation uses these; they should not accumulate in the Blocked column):
- `blocked:repo`
- `blocked:deps`
- `blocked:exclusive`
- `blocked:thrash`
- `blocked:artifact`

Review tags:
- `review:auto` (orchestrator is allowed to auto-review)
- `review:pending` (needs reviewer spawned)
- `review:inflight` (reviewer running)
- `review:pass` (last review passed threshold)
- `review:rework` + `needs-rework` (last review failed)
- `review:blocked:wip` (rework is waiting for WIP capacity)
- `review:error` (review runner/auth/quota failed)
- `review:rerun` / `review:retry` (explicit human request to rerun review)

---

## Run Model (The Core Design)

Everything active is a **run** with a unique `runId` and a dedicated directory.

Run roots:
- Workers: `/Users/joshwegener/clawd/runs/worker/task-<id>/<runId>/`
- Reviewers: `/Users/joshwegener/clawd/runs/review/task-<id>/<runId>/`

Worker run files:
- `worker.log` (human debug only)
- `patch.patch` (the patch to review/apply)
- `kanboard-comment.md` (ready-to-post comment text)
- `meta.json` (spawn metadata)
- `done.json` (canonical completion signal)

Reviewer run files:
- `review.log` (human debug only)
- `meta.json` (spawn metadata)
- `review.json` (canonical completion signal)

Control flow rule:
- The orchestrator must never infer completion from a stale file in a previous run directory.
- Workers: only accept completion via the *recorded* run entry’s `done.json` (`donePath` stored in state).
- Reviewers: accept completion via `review.json` from either:
  - the recorded run entry’s `resultPath`, or
  - recovery mode: the most recent `runs/review/task-<id>/*/review.json` that matches the current patch revision.

---

## Task Lifecycle (State Machine)

### Backlog → Ready
The orchestrator keeps `Ready` stocked.

Selection rules:
- Skip held/no-auto.
- Enforce deps/exclusive.
- Enforce repo mapping (unless `no-repo`).

If deterministically blocked:
- Keep it in `Backlog` and add a durable tag (do not fill the Blocked column):
  - `blocked:repo` / `blocked:deps` / `blocked:exclusive` / `blocked:thrash`

### Ready → WIP (Spawn Worker)
Invariant: no task enters WIP unless a worker handle is recorded.

Spawn command:
- `BOARD_ORCHESTRATOR_WORKER_SPAWN_CMD` → `scripts/spawn_worker_tmux.sh`

Spawn stdout contract (single JSON object):
```json
{
  "execSessionId": "tmux:clawd:worker-42",
  "runId": "20260203T103000Z-acde12",
  "runDir": "/Users/joshwegener/clawd/runs/worker/task-42/20260203T103000Z-acde12",
  "logPath": ".../worker.log",
  "patchPath": ".../patch.patch",
  "commentPath": ".../kanboard-comment.md",
  "donePath": ".../done.json",
  "startedAtMs": 1738589000000
}
```

Worker completion contract:
- Worker writes `done.json` at end (always), and must produce:
  - a non-empty patch file
  - a non-empty Kanboard comment file

If the worker cannot be started:
- Tag `paused` + `paused:missing-worker`
- Move the card to `Blocked` (temporary policy to keep Ready/WIP clean)

### WIP → Review (Worker Completion)
The orchestrator moves WIP → Review only when the current run’s `done.json` is present and valid:
- `ok == true`
- `patchExists == true`
- `commentExists == true`
- `patchBytes > 0`

When moving WIP → Review:
- Add `review:auto` + `review:pending`
- Post `kanboard-comment.md` as a Kanboard comment (best-effort)
- Kill the worker tmux window `worker-<id>` (cleanup)

If `done.json` exists but artifacts are unusable (empty/missing/non-zero exit):
- Keep it out of Review and prevent thrash:
  - move to `Backlog` and tag `blocked:artifact`

### Review (Spawn Reviewer, Read review.json)
Spawn command:
- `BOARD_ORCHESTRATOR_REVIEWER_SPAWN_CMD` → `scripts/spawn_reviewer_tmux.sh`

Spawn stdout contract:
```json
{
  "execSessionId": "tmux:clawd:review-42",
  "runId": "20260203T104500Z-acde12",
  "runDir": "/Users/joshwegener/clawd/runs/review/task-42/20260203T104500Z-acde12",
  "logPath": ".../review.log",
  "resultPath": ".../review.json",
  "startedAtMs": 1738589100000
}
```

Reviewer output contract:
- Reviewer writes strict JSON to `review.json`:
```json
{
  "score": 91,
  "verdict": "PASS",
  "critical_items": [],
  "notes": "short summary",
  "reviewRevision": "abcd1234..."
}
```

Decision policy:
- PASS requires: `score >= BOARD_ORCHESTRATOR_REVIEW_THRESHOLD` and `verdict == "PASS"` and `critical_items` empty.
- Otherwise REWORK/BLOCKER.

On PASS:
- Tag `review:pass`
- Optionally auto-move Review → Done (config `REVIEW_AUTO_DONE`)
- Kill tmux window `review-<id>`

On REWORK/BLOCKER:
- Tag `review:rework` + `needs-rework`
- Move Review → WIP if capacity allows (or tag `review:blocked:wip` until capacity frees)
- Ensure a new worker run will be spawned (new `runId` means no stale completion)
- Kill tmux window `review-<id>`

Reviewer errors:
- If `review:error` is present and there is no stored result, do not respawn automatically.
- Only rerun review on explicit human tags: `review:rerun` or `review:retry`.
- If a reviewer exits without producing `review.json` (no result payload), tag `review:error` + comment with `runDir`/`logPath` and stop.

Thrash guard:
- If the same patch revision fails review too many times within the window, stop looping:
  - move to `Backlog` tagged `blocked:thrash`

---

## Critical Mode

When a non-held `critical` task exists:
- Prioritize starting/advancing that card.
- When a critical is actively in WIP:
  - tag non-critical WIP cards `paused` + `paused:critical` (tag-based; do not move columns)
  - do not pull new non-critical work
- Critical tasks can exceed the normal WIP limit.

When no critical remains:
- Clear `paused:critical` (and `paused` only if it was added solely for critical preemption).

---

## Operations (tmux)

Bring up tmux + orchestrator loop:
- `/Users/joshwegener/clawd/scripts/clawd_up.sh`

Stop automation:
- `/Users/joshwegener/clawd/scripts/clawd_down.sh`

Attach:
- `tmux attach -t clawd`

Useful windows:
- `orchestrator` (the loop)
- `orchestrator-logs` (tails `/Users/joshwegener/clawd/memory/orchestrator.log`)
- `worker-logs` (tails latest worker run logs under `runs/worker/`)
- `review-logs` (tails latest reviewer run logs under `runs/review/`)

Cleanup behavior:
- After a run completes, the orchestrator kills `worker-<id>` / `review-<id>` windows (configurable).

---

## State File Contract

State path:
- `/Users/joshwegener/clawd/memory/board-orchestrator-state.json`

Treat as an API; important keys:
- `lastActionsByTaskId` (cooldown)
- `repoByTaskId`, `repoMap`
- `workersByTaskId` (stores the current worker run entry per task, including `donePath`)
- `reviewersByTaskId` (stores the current reviewer run entry per task, including `resultPath`)
- `reviewResultsByTaskId` (stored parsed results)
- `pausedByCritical` (who we paused and why)
- `reviewReworkHistoryByTaskId` (thrash guard history)

---

## “Working” Acceptance Criteria

1) WIP → Review can only happen after a valid `done.json` from the current run.
2) Review decisions come only from `review.json` (recorded run or recovery-mode latest matching revision), and are idempotent.
3) No WIP ↔ Review ping-pong without new worker output (new runId) or explicit human tags.
4) Review errors do not thrash (require `review:rerun` / `review:retry`).
5) tmux windows are observable and cleaned up on completion (no hundreds of orphan panes).
