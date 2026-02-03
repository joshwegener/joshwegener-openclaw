# RecallDeck Board Orchestrator — Playbook (v1)

Purpose: Keep the Kanban board aligned with reality and keep work moving autonomously.

This playbook is written to be machine-followable: clear rules, explicit safety valves, and minimal guessing.

## Board model (actual columns)

Column titles (Kanboard):
- Backlog
- Ready
- Work in progress (WIP)
- Review
- Blocked
- Done

Definitions:
- **WIP** means active coding/execution. **Hard limit = 2** (except temporary overflow when a review failure must return to WIP; when WIP > 2, do not pull new work).
- **Review** is separate and can be unlimited.

State path:
- Default: `/Users/joshwegener/clawd/memory/board-orchestrator-state.json`
- Override: `BOARD_ORCHESTRATOR_STATE` (preferred), with `RECALLDECK_STATE_PATH` / `STATE_PATH` as legacy fallbacks

## Tags

- `epic` = epic container card
- `story` + `epic-child` = child task that belongs to an epic
- `hold` (or `no-auto`) = **escape hatch**: orchestrator must not move/start this task
- `docs-required` = requires a docs companion task
- `exclusive:<key>` = only one task with this key can be in WIP at a time (e.g. `exclusive:server-db`)

## Dependencies

Add a line in any task description:
- `Depends on: #<taskId>, #<taskId>`

The orchestrator must not move/start a task until all dependencies are in **Done**.

Epic linkage:
- For each child task, add a note/comment: `Epic: #<epicId> <epicTitle>`

## Repo mapping (required for auto-start)
The orchestrator only auto-starts tasks (Ready → WIP) when it can map the task to a local repo.

Provide a mapping via one of:
- Tag: `repo:<key>` (e.g. `repo:server`, `repo:RecallDeck-Server`)
- Description line: `Repo: <key-or-path>`
- Title prefix: `<key>:` (e.g. `server: ...`, `web: ...`) **legacy fallback**

If a task truly has no repo (planning/research), add tag `no-repo` so it can still be started.

Note: Prefer tags + explicit `Repo:` hints; title-prefix mapping is legacy and can be disabled via `BOARD_ORCHESTRATOR_ALLOW_TITLE_REPO_HINT=0`.

## Auto-block + auto-heal (self-healing)
When a task can’t be started for a deterministic reason, the orchestrator may move it to `Blocked` and tag it:
- `auto-blocked`
- plus one reason tag: `blocked:deps`, `blocked:exclusive`, or `blocked:repo`

When the reason clears, the orchestrator auto-heals the task from `Blocked` back to `Ready` and removes those tags.

## Epic breakdown rule (idempotent)

- Epic container cards are **not** coding tasks.
- If the orchestrator is about to pull an epic and it has no children yet, it creates exactly one breakdown task:
  - Title format: `Break down epic #<epicId>: <epicTitle>`
  - If a breakdown task with that exact title already exists and is not Done, **do not create another**.
- The breakdown task is eligible for Ready/WIP like any other task and counts toward WIP.
- The epic card stays as the container; it moves to Done when all children are Done.

## Swimlanes (MVP)

MVP behavior:
- Prefer the **Default swimlane**.
- If/when multiple swimlanes matter, introduce an explicit priority list in state.

## Worker tracking (leases)

Leases are canonical; PID is used for liveness checks (best-effort).
- Per-task lease root: `RECALLDECK_WORKER_LEASE_ROOT` (default `/tmp/recalldeck-workers`)
- Active lease: `task-<id>/lease/lease.json` (contains worker pid/paths/run id)
- State `workersByTaskId` is a cache rebuilt from leases when enabled (`BOARD_ORCHESTRATOR_USE_LEASES=1`).
- Missing/failed workers are reconciled deterministically:
  - Alive lease → no spawn.
  - Dead lease → archive + respawn (thrash guard).
  - Unknown lease → alert + avoid aggressive respawn.
- Pausing is tag-based and should always include a reason tag when automated (e.g. `paused:missing-worker`, `paused:critical`).
- The Paused column is optional/legacy; prefer keeping cards in place and using tags.
- A task is only moved into WIP when a worker handle can be recorded immediately (no silent WIP).
- If a worker log shows a completed output (patch marker / kanboard comment file), auto-move WIP → Review.

## Safety valves

- **Locking:** OS-level lock (`flock`) on `/tmp/board-orchestrator.lock` (no stale TTL). Optional fallback: `BOARD_ORCHESTRATOR_LOCK_STRATEGY=legacy-stale-file`.
- **Action budget:** max 3 actions per run (moves/creates) to avoid thrash.
- **Cooldown:** allow multiple transitions in a *single run* (e.g., Backlog→Ready→WIP), but don’t repeatedly re-move the same task across runs (30m cooldown per task).
- **Never auto-Done (MVP):** orchestrator may move WIP→Review, but should not mark Done based on inference.

## Orchestrator loop (every 15 minutes)

1) Read board state.
2) If WIP > 2: do not pull new work; focus only on notifying about stale/unknown WIP.
   - Reconcile WIP tasks with completed worker output (auto-advance to Review).
   - Reconcile WIP tasks missing worker handles (auto-spawn or auto-pause).
3) If WIP < 2:
   - Ensure Ready has work:
     - If Ready empty: promote top Backlog task by position (skip `hold`).
     - If top Backlog is `epic`: create (or find) breakdown task, then promote breakdown task.
   - Start work:
     - Move top Ready → WIP.
4) Escalation:
   - If any task is moved to Blocked, message Josh immediately with what is needed.
5) Notifications:
   - Only message when actions are proposed/executed or when a blocker is detected.

## Dry-run arming

First run should be dry-run (propose actions only), then auto-arm to live unless Josh says otherwise.
