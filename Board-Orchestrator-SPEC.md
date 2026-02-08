# RecallDeck Board Orchestrator — SPEC (MVP v1)

## Goal
Keep the Kanban board as the source of truth and keep work moving automatically.

MVP philosophy: **do the safe, deterministic things** (move cards, start workers, record state, escalate blockers). **Do not** infer completion by probing work artifacts (worktrees/tests/PRs) yet.

## Board model
Project: `RecallDeck` (Kanboard)

Columns (by title):
- `Backlog`
- `Ready`
- `Work in progress`
- `Review`
- `Documentation`
- `Blocked`
- `Done`

Definitions:
- **WIP** = active coding/execution. **Hard limit = 2**.
- **Review** = separate lane, unlimited.
- **Documentation** = final gate; cards require `docs:completed` or `docs:skip` to move to Done.
- If a review fails and a task must return to WIP, WIP may temporarily exceed 2, but the orchestrator must **stop pulling new work** until WIP <= 2.

Ordering:
- “Top of list” = Kanboard `position` (lowest number is highest priority).

Swimlanes (MVP):
- Prefer `Default swimlane`.

## Tags
- `epic` — epic container card (not a coding task)
- `story` + `epic-child` — child task belonging to an epic
- `hold` or `no-auto` — **escape hatch**: orchestrator must not move/start this task
- `docs-required` — this task requires a docs update task before it can be considered complete
- Documentation workflow (orchestrator-owned):
  - `docs:auto` — docs automation enabled for this card
  - `docs:pending` — card is waiting for docs work
  - `docs:inflight` — docs worker running
  - `docs:completed` — docs updated (gate to Done)
  - `docs:skip` — docs intentionally skipped (gate to Done)
  - `docs:error` — docs automation failed; no auto-respawn
  - `docs:retry` — explicit human intent to retry after fixing environment

## Dependencies + exclusivity (new)
Dependencies convention (preferred): add a line in the task description:
- `Depends on: #<taskId>, #<taskId>`

Exclusive work convention:
- Tag tasks with `exclusive:<key>` (e.g. `exclusive:server-db`) *or* add in description:
  - `Exclusive: <key>`

Rules:
- A task cannot be promoted to Ready/WIP until all `Depends on` tasks are in **Done**.
- If a task has an exclusive key, only one task with that key may be in WIP at once.

Linking epic children:
- Each child task should get a comment: `Epic: #<epicId> <epicTitle>`

## Repo mapping (worker start)
When the orchestrator is about to start a task (Ready → WIP), it must be able to map the task to a local repo.

Supported hints (first match wins):
- Tag: `repo:<key>` (e.g. `repo:server`, `repo:RecallDeck-Server`)
- Description line: `Repo: <key-or-path>` (e.g. `Repo: server` or `Repo: /Users/joshwegener/Projects/RecallDeck/RecallDeck-Server`)
- Tag: `no-repo` (explicit opt-out for planning/research tasks)
- Title prefix: `<key>:` is **legacy** and is **not sufficient** for automation (use an explicit tag or `Repo:` line)

Notes:
- Prefer tags + explicit `Repo:` hints; title-prefix mapping is legacy and can be disabled via `BOARD_ORCHESTRATOR_ALLOW_TITLE_REPO_HINT=0`.
- If a task is missing `Repo:`/`repo:<key>`/`no-repo`, the orchestrator tags `hold:needs-repo` and leaves a comment with fix instructions.

Defaults:
- The orchestrator auto-discovers repos under `RECALLDECK_REPO_ROOT` (default: `/Users/joshwegener/Projects/RecallDeck`) and adds common aliases (e.g. `api` → `server`).
- If a task truly has no repo (planning/research), add tag `no-repo` to bypass mapping.

## Auto-block + auto-heal (self-healing)
If a task cannot be started due to a deterministic constraint, the orchestrator may move it to `Blocked` and tag it:
- `auto-blocked`
- plus one reason tag: `blocked:deps`, `blocked:exclusive`, or `blocked:repo`

When the constraint clears (deps done, exclusive freed, repo mapping available), the orchestrator will auto-heal the task from `Blocked` back to `Ready` and remove those tags.

## State + safety
State file (cache): `/Users/joshwegener/clawd/memory/board-orchestrator-state.json`

State path env:
- `BOARD_ORCHESTRATOR_STATE` (preferred)
- `RECALLDECK_STATE_PATH` / `STATE_PATH` (legacy fallbacks used by some monitors)

Canonical worker identity lives in per-task leases:
- Root: `RECALLDECK_WORKER_LEASE_ROOT` (default `/tmp/recalldeck-workers`)
- Active lease: `task-<id>/lease/lease.json`
- History (thrash guard): `task-<id>/history.json`
- Archive: `task-<id>/archive/<leaseId>/`

If leases are enabled (`BOARD_ORCHESTRATOR_USE_LEASES=1`, default), the orchestrator rebuilds
`workersByTaskId` from the lease metadata each run. The state file is a convenience cache only.

Schema (MVP):
```json
{
  "dryRun": true,
  "dryRunRunsRemaining": 1,
  "lastActionsByTaskId": {"28": 1769730000000},
  "swimlanePriority": ["Default swimlane"],
  "workersByTaskId": {
    "28": {
      "kind": "codex",
      "execSessionId": "<clawdbot-exec-sessionId>",
      "logPath": "/Users/joshwegener/clawd/memory/worker-logs/task-28.log",
      "repoKey": "server",
      "repoPath": "/Users/joshwegener/Projects/RecallDeck/RecallDeck-Server",
      "startedAtMs": 1769730000000,
      "leaseId": "20260131T101500Z-acde1234"
    }
  }
}
```

Locking:
- Lock file: `/tmp/board-orchestrator.lock`
- Default strategy: OS-level lock (`flock`); no TTL guesswork.
- Optional fallback: `BOARD_ORCHESTRATOR_LOCK_STRATEGY=legacy-stale-file` (10m stale).
- `BOARD_ORCHESTRATOR_LOCK_WAIT_MS` controls wait time before giving up (default 0).

Action budget:
- Max 3 actions per run (moves/creates)

Cooldown:
- Prevent repeated moves across runs for the same task: 30 minutes.
- Exception: `Ready -> WIP` should **not** be blocked by cooldown.
- Implementation note: cooldown should be evaluated against the prior-run timestamps (snapshot at tick start) so a single run can still do `Backlog -> Ready -> WIP`.

Dry-run arming:
- First run is DRY RUN.
- After that, auto-arm to LIVE.

## Epic breakdown (idempotent)
When the top Backlog item is tagged `epic`:
- The orchestrator must **not** move the epic card into WIP.
- It must create (exactly once) a breakdown task:
  - Title: `Break down epic #<epicId>: <epicTitle>`
  - If a task with that exact title already exists and is not Done, **do not create another**.
- Breakdown task is eligible like any other task and counts toward WIP.

## Orchestrator responsibilities (MVP)
Runs every 15 minutes.

1) Read board state.
2) If WIP count > 2: do not pull new work; notify.
3) If WIP count < 2:
   - Ensure Ready has work:
     - If Ready empty, promote top Backlog item to Ready (skip `hold/no-auto`).
     - If top Backlog is `epic`, create/find breakdown task and (in MVP) leave it in Backlog unless explicitly promoted.
   - Move top Ready item into WIP.
4) Worker start:
   - For every task moved into WIP, acquire the per-task lease and start a worker (Codex).
   - If `BOARD_ORCHESTRATOR_WORKER_SPAWN_CMD` is configured, the orchestrator will attempt to spawn and record the handle immediately.
   - WIP tasks missing a worker handle are reconciled via leases:
     - If lease is alive: no spawn.
     - If lease is dead: archive lease + respawn (subject to thrash guard).
     - If lease is unknown: alert + avoid aggressive respawn.
   - Thrash guard: `BOARD_ORCHESTRATOR_THRASH_WINDOW_MIN` + `BOARD_ORCHESTRATOR_THRASH_MAX_RESPAWNS` → pause + tag (default `paused:thrash`).
   - If a task is moved to WIP but no worker handle can be recorded, it is auto-paused to avoid silent WIP.
   - If a worker log (from `BOARD_ORCHESTRATOR_WORKER_LOG_DIR`) shows a completed output (patch marker / kanboard comment file), the orchestrator auto-moves WIP → Review.
   - If no safe repo mapping is available, do **not** start; instead comment and/or move to Blocked.
5) Escalation:
   - Anything truly blocked on Josh must go to `Blocked` and message Josh immediately.

6) Documentation (when column exists):
   - On Review PASS, move Review -> Documentation and tag `docs:auto` + `docs:pending`.
   - If `BOARD_ORCHESTRATOR_DOCS_SPAWN_CMD` is configured and the card has `docs:auto` + `docs:pending`, spawn a docs worker (Codex) that updates `RecallDeck-Docs`.
   - On docs completion:
     - non-empty docs patch -> tag `docs:completed`
     - empty docs patch -> tag `docs:skip`
     - post docs summary comment and move Documentation -> Done
   - On failure or unusable artifacts, tag `docs:error` and require `docs:retry` to retry (avoid thrash).

## Script interface
Python script: `/Users/joshwegener/clawd/scripts/board_orchestrator.py`

- If nothing to do: output exactly `NO_REPLY`.
- Otherwise output exactly one line of JSON:
```json
{
  "mode": "DRY_RUN" | "LIVE",
  "actions": ["..."],
  "promotedToReady": [28],
  "movedToWip": [28],
  "createdTasks": [99],
  "errors": []
}
```

## Cron behavior
Cron job `RecallDeck board orchestrator (15m)`:
- Runs python script.
- If output is NO_REPLY: do nothing.
- Else parse JSON.
- If `mode == LIVE` and `movedToWip` has ids:
  - Start Codex worker(s) in the appropriate repo.
  - Comment on the Kanboard task with the worker handle.
  - Persist mapping in `workersByTaskId`.
- Send Josh one Telegram message summarizing:
  - moves performed
  - which tasks had workers started (session ids)
  - any errors.
