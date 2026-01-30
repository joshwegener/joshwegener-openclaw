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

## Worker tracking (v1 approach)

Do not rely on OS PIDs.
- Track a worker handle and liveness in state:
  - `taskId -> { handle, startedAtMs, lastSeenAtMs }`
- Liveness is **TTL-based**:
  - Alive if `now - lastSeenAtMs < 60 minutes`
  - If stale/missing: treat as “unknown” and escalate (MVP: Block + message Josh), rather than guessing completion.

## Safety valves

- **Locking:** only one orchestrator run at a time (lock file; stale after 10 minutes).
- **Action budget:** max 3 actions per run (moves/creates) to avoid thrash.
- **Cooldown:** allow multiple transitions in a *single run* (e.g., Backlog→Ready→WIP), but don’t repeatedly re-move the same task across runs (30m cooldown per task).
- **Never auto-Done (MVP):** orchestrator may move WIP→Review, but should not mark Done based on inference.

## Orchestrator loop (every 15 minutes)

1) Read board state.
2) If WIP > 2: do not pull new work; focus only on notifying about stale/unknown WIP.
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
