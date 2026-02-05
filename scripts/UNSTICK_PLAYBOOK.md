# Unstick Playbook (RecallDeck Orchestrator)

Use this when the Kanboard pipeline looks “stuck” (no movement for a while, WIP slots blocked, workers missing, etc.).

## 1) Identify the stuck task(s)
- In Kanboard, note the task id(s) and current column(s): **Ready**, **WIP**, **Review**, **Blocked**.
- Check tags for the most common “stop” causes:
  - `paused:*` (manual pause / safety pause)
  - `hold:*` / `no-auto`
  - `auto-blocked` + `blocked:*`
  - `review:*`
  - `docs:*`

## 2) If a task is in WIP but not progressing
### A) Missing worker
Symptoms:
- Task in **WIP** tagged `paused:missing-worker`

Actions:
- Ensure the repo mapping is present (`Repo:` line or `repo:<key>` tag).
- Ensure the worker spawn command is configured (`BOARD_ORCHESTRATOR_WORKER_SPAWN_CMD`).
- Clear `paused` / `paused:missing-worker` after fixing config to allow a retry.

### B) Stale worker log (watchdog)
Symptoms:
- Task in **WIP** tagged `paused:stale-worker`

Meaning:
- The worker pid was considered alive, but its log hasn’t updated for `BOARD_ORCHESTRATOR_WORKER_LOG_STALE_MS`.
  The orchestrator pauses the card to prevent WIP deadlocks (it does **not** auto-respawn a live pid).

Actions:
- Inspect the worker log and lease info:
  - Worker log: `memory/worker-logs/task-<id>.log`
  - Lease: `/tmp/recalldeck-workers/task-<id>/lease/lease.json`
- If the worker is truly hung, terminate it and remove/archive the lease directory, then clear `paused:stale-worker` to allow a fresh spawn.

## 3) Critical-mode deadlock checks
Expected behavior:
- A `critical` card is **exclusive only while it is in WIP**.
- Once the critical reaches **Review**, normal work should resume.

If non-critical WIP cards are stuck paused by a critical:
- Look for `paused:critical` and/or state entries under `pausedByCritical`.
- The orchestrator should clear `paused:critical` automatically when no critical is currently in WIP.

## 4) Quick diagnostics / commands
- Run orchestrator once (prints JSON or `NO_REPLY`):
  - `python3 scripts/board_orchestrator.py`
- Check for orphan workers (live pid but task not in WIP):
  - Look for `manual-fix: ... orphan` warnings in orchestrator output.

## 5) If a task is in Documentation but not progressing
Symptoms:
- Task is in **Documentation** with `docs:pending` (and often `docs:auto`) for a long time.

Actions:
- Ensure docs automation is enabled:
  - `BOARD_ORCHESTRATOR_DOCS_SPAWN_CMD` must be configured in the orchestrator env.
  - Quick toggle helpers:
    - enable: `scripts/docs_on.sh`
    - disable: `scripts/docs_off.sh`
- If the card is tagged `docs:error`:
  - Read the docs worker run dir/log referenced in the card comment.
  - Fix the docs environment (docs repo path, Codex CLI, Kanboard env).
  - Add tag `docs:retry` to allow a retry (or manually tag `docs:skip` / `docs:completed` to finish).

