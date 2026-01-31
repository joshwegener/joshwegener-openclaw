# RecallDeck Kanban Orchestration North Star (clawd)

This document is the end-to-end North Star for how the RecallDeck Kanban board, orchestrator, workers, reviewers, and monitors are supposed to work together.

This repo (`/Users/joshwegener/clawd`) is the local “ops brain” for the board automation.

It is written as a contract: the code and cron jobs should converge to match this.

---

## 0) Scope and principles

### 0.1 Scope
This North Star covers:
- Kanboard project `RecallDeck` and its columns/swimlanes/tags conventions
- Orchestrator behavior (`scripts/board_orchestrator.py`)
- Worker spawn contract (Codex) (`scripts/spawn_worker_codex.sh`)
- Critical-mode invariants (`scripts/critical_monitor.py`)
- Safety checks (`scripts/overnight_safety_check.py`)
- Review lane automation (reviewer spawn + score + verdict + rework loop)

Out of scope (but referenced where it interacts):
- The actual RecallDeck product repos (`/Users/joshwegener/Projects/RecallDeck/*`)
- The Gateway cron definitions (they live in Clawdbot config, not in this repo)

### 0.2 Principles
- The board is the source of truth; the orchestrator is a deterministic synchronizer.
- No silent states: WIP implies active execution, Review implies active review.
- Deterministic over clever: when unsure, tag + comment + stop.
- Tag-based “pause” is preferred to moving columns; keep card position intact.
- Critical work supersedes throughput.

---

## 1) Board model (canonical)

### 1.1 Project
- Kanboard project name: `RecallDeck`

### 1.2 Columns (canonical titles)
The orchestrator keys off exact column titles. Canonical titles:
- `Backlog`
- `Ready`
- `Work in progress` (WIP)
- `Review`
- `Blocked`
- `Done`

Notes:
- A `Paused` column is **legacy/optional**. The North Star is tag-based pause.

### 1.3 Swimlanes
- Primary swimlane: `MVP`
- Other swimlanes: `Ops`, `Later`, plus Kanboard’s default.
- Orchestrator uses a `swimlanePriority` list in state for stable ordering.

---

## 2) Tag model (canonical)

Tags are the control surface. Keep them stable and few.

### 2.1 Escape hatches (never override)
- `hold` or `no-auto`: orchestrator must not move/start the card.

### 2.2 Repo mapping
To start execution, a task must map to a local repo (unless explicitly exempted).

Accepted repo hints (first match wins):
1) Tag: `repo:<key>`
2) Description line: `Repo: /absolute/path` or `Repo: <key>`
3) (Legacy, optional) Title prefix: `<key>:` (behind `BOARD_ORCHESTRATOR_ALLOW_TITLE_REPO_HINT`)

Exemptions:
- `no-repo`: task does not require a repo (planning/meta). Still can be reviewed.

### 2.3 Dependencies
- Description line: `Depends on: #31, #32` (also accepts `Dependencies:` / `Dependency:`)

### 2.4 Exclusivity
- Tag: `exclusive:<key>` and/or description line `Exclusive: key1, key2`

### 2.5 Critical + pause semantics
- `critical`: this task supersedes everything.

Pause tags:
- `paused` (generic/manual fallback)
- Always prefer a reason tag when the system applies a pause:
  - `paused:critical`
  - `paused:missing-worker`
  - `paused:deps`
  - `paused:exclusive`
  - (future) `paused:review-rework`

Key rule:
- A paused card stays in its column and keeps its position.

### 2.6 Review tags (North Star)
- `review:auto`: orchestrator is allowed to auto-review this card
- `review:pending`: needs reviewer spawned
- `review:inflight`: reviewer is running
- `review:pass`: last review passed threshold
- `review:rework`: last review failed threshold / verdict != PASS
- `review:blocked:wip`: review says rework but WIP is currently full
- `review:error`: reviewer failed / result parse failed
- `review:skip`: never auto-review this card
- `review:rerun`: force a rerun even if unchanged

---

## 3) Automation loops (cron “actors”)

### 3.1 Board orchestrator (15m)
Script: `scripts/board_orchestrator.py`

Contract:
- Emits `NO_REPLY` when no action is needed.
- Otherwise emits a single JSON object with actions and errors.

Safety valves:
- Lock file (OS-level lock): `/tmp/board-orchestrator.lock` (`flock` by default).
- Action budget per run (prevents thrash)
- Cooldown per task via `lastActionsByTaskId`

### 3.2 Critical monitor (every ~2m)
Script: `scripts/critical_monitor.py`

Contract:
- `NO_REPLY` when healthy.
- `ALERT: ...` when invariants are violated.

### 3.3 Overnight safety check (hourly)
Script: `scripts/overnight_safety_check.py`

Contract:
- `NO_REPLY` when healthy.
- `ALERT: ...` summarizing stalls/drift.

### 3.4 Quota guardrails (every ~10m)
Not implemented as code in this repo; state lives at:
- `memory/quota-guardrail-state.json`

North Star requirement:
- Alerts include reset weekday + time remaining (ticket #61).

---

## 4) Task lifecycle (end-to-end)

### 4.1 Backlog → Ready
Orchestrator keeps Ready stocked.

Selection rules:
- Skip `hold` / `no-auto` / `paused:*`.
- Skip epic containers.
- Enforce dependencies: don’t start until deps are Done.
- Enforce exclusivity: don’t start if the key already exists in WIP.
- Enforce repo mapping (unless `no-repo`).

If a task is deterministically blocked:
- Move it to `Blocked`.
- Tag it `auto-blocked` plus one of:
  - `blocked:deps`
  - `blocked:exclusive`
  - `blocked:repo`

### 4.2 Ready → WIP (worker spawning)
Invariant: **No silent WIP**.

Rule:
- A task is only moved Ready → WIP if a worker handle can be recorded immediately.

Worker spawn contract:
- Orchestrator uses `BOARD_ORCHESTRATOR_WORKER_SPAWN_CMD`.
- The spawn command must output JSON to stdout:
  - `{"execSessionId":"...","logPath":"..."}`

Default implementation in this repo:
- `scripts/spawn_worker_codex.sh {task_id} {repo_key} {repo_path}`
- It backgrounds `codex exec` and logs to `memory/worker-logs/task-<id>.log`.

Failure:
- If a worker cannot be started, the task stays in Ready and is tagged:
  - `paused` + `paused:missing-worker`

### 4.3 WIP steady-state (reconciliation)
A task in WIP must have:
- a repo mapping (unless `no-repo`), and
- an active worker lease (`task-<id>/lease/lease.json`) when leases are enabled.

`workersByTaskId` is treated as a cache rebuilt from leases each run (when enabled).

If a WIP task is missing a worker lease or the lease is dead:
- Attempt a respawn (subject to thrash guard).
- Otherwise tag `paused` + `paused:missing-worker` and alert.

### 4.4 WIP → Review (completion detection)
Contract:
- Worker completion is detected conservatively by scanning the worker log tail.
- The worker must emit a patch marker, and the patch file must exist on disk.

When completion is detected:
- Orchestrator moves WIP → Review.
- It records `completedAtMs` and `patchPath` into the worker entry.

### 4.5 Review automation (Claude scoring + rework loop)
North Star behavior:

1) When a task enters Review (and has `review:auto`, or is auto-moved there by the orchestrator):
- Add `review:pending`.
- Spawn a reviewer (Claude/opus) and add `review:inflight`.

2) Reviewer output contract:
- Reviewer writes a single machine-parseable result line near the end:
  - `REVIEW_RESULT: {"score":87,"verdict":"PASS",...}`
- Or it outputs strict JSON that includes `score` + `verdict`.

3) Decision policy:
- Score threshold default: 85.
- PASS: `score >= threshold` and verdict PASS.
- Otherwise: REWORK.

4) Posting results:
- Orchestrator posts a comment back to the Kanboard card containing the review JSON block.

5) Rework loop:
- If REWORK:
  - Tag `review:rework`.
  - If WIP capacity allows, move Review → WIP and spawn a fixer worker.
  - If WIP is full, tag `review:blocked:wip` and retry next tick.

6) Priority rule:
- Before pulling from Ready, orchestrator must first service Review rework (Review → WIP) when WIP has capacity.

### 4.6 Blocked auto-heal
Auto-heal rule:
- When Ready is empty (anti-thrash) and a previously auto-blocked task’s constraint clears:
  - move Blocked → Ready
  - remove `auto-blocked` + reason tags

---

## 5) Critical mode (supersedes everything)

### 5.1 When critical mode is active
If any non-held `critical` task is not Done:
- The orchestrator must prioritize moving that critical task forward.

### 5.2 Preemption behavior
- When a critical is in WIP or Review:
  - Tag all non-critical WIP tasks: `paused` + `paused:critical`
  - Do not pull any new non-critical work from Ready.

### 5.3 Capacity rules during critical
- Critical can enter WIP even if WIP is “full”.
- This is implemented by pausing non-critical WIP (tag-based), freeing “active WIP” capacity.

### 5.4 Exiting critical
When no critical tasks remain:
- Remove `paused:critical` from tasks that were auto-paused.
- Remove `paused` only if it was added solely for critical preemption.

---

## 6) State file contract

Path:
- `memory/board-orchestrator-state.json`

Treat this file as an API: keys are stable and backwards-compatible.

Core keys:
- `lastActionsByTaskId` (cooldown)
- `swimlanePriority`
- `repoMap` (discovered + persisted)
- `repoByTaskId`
- `workersByTaskId`
- `pausedByCritical` (who we paused and why)
- `autoBlockedByOrchestrator`

Review keys (North Star):
- `reviewersByTaskId`
- `reviewResultsByTaskId`

---

## 7) Acceptance criteria (what “working” means)

1) If WIP contains a non-paused task, it has a live worker handle.
2) Ready → WIP never happens unless a worker handle is recorded immediately.
3) Worker completion reliably advances WIP → Review.
4) Review is actively serviced:
   - reviewer spawned automatically for Review cards
   - results posted back to Kanboard
   - rework loop feeds back into WIP before pulling from Ready
5) Critical always preempts, without deadlocking:
   - critical does not get stuck behind held/unstartable prerequisites
   - non-critical WIP is paused via tags (not moved) and resumes after
6) No infinite thrash loops (budget + cooldown + tag-based locks).
7) Monitoring is actionable:
   - critical_monitor only alerts on true invariant violations
   - safety check alerts correlate to real stalls and include next actions

---

## 8) Known gaps / follow-ups (as of now)

- Automated review loop (reviewer spawning + scoring + rework) must be fully implemented (ticket #60).
- Quota guardrail messages should include reset weekday + time remaining (ticket #61).
- Ensure repoMap always includes `clawd` (or derive it deterministically) so `repo:clawd` works.
- Ensure the Kanboard comment API is consistently used for worker/reviewer outputs.
- Verify the exact Kanboard column titles match code constants (avoid “Doing” drift).

---

## 9) Next step (audit ticket)
After this North Star stabilizes, create a CRITICAL audit ticket that:
- walks every cron + script + tag/column behavior
- confirms each acceptance criterion is enforced
- documents any remaining mismatch as follow-up tasks
