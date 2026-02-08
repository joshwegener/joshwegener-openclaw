# RecallDeck Orchestrator Playbook (Ops)

Purpose: quick, deterministic steps when the board automation gets into a weird state.

Principles
- Prefer *system fixes* over manual edits.
- If a manual fix is required, immediately create a **CRITICAL root-cause ticket** that:
  - links the **problem ticket(s)** as live examples
  - describes the observed failure mode
  - defines deterministic acceptance criteria
- Use tags and explicit `Repo:` hints; avoid heuristics based on title parsing.

---

## 0) Fast triage checklist (2 minutes)
1) What’s the active critical (if any)?
2) What’s in WIP (and is WIP > limit)?
3) Any cards in WIP missing worker handles?
4) Any blocked cards that should auto-heal (deps done / exclusives freed / repo mapping fixed)?
5) Is there an obvious "done but not merged" situation (patch produced, board not advanced)?

---

## 1) CRITICAL ticket created but not running
**Symptom**: critical ticket exists but remains in Backlog/Ready/Blocked.

**Checks**
- Does it have a valid repo mapping?
  - Add `Repo: /Users/joshwegener/clawd` (or tag `repo:<key>`).
- Does it have `Depends on:` lines that prevent starting?
  - Remove `Depends on:` if the goal is immediate preemption.
  - Instead add a “Related: #X” section.

**Rule**
- Critical preemption should only pause others after verifying the critical can start.

---

## 2) Critical is in WIP but has NO worker handle
**Symptom**
- `critical_monitor` or safety check says: “Active critical #X is in WIP but has no worker handle recorded in state”.

**Immediate action (allowed manual intervention)**
- Start the worker for the critical ticket.
- Record the worker handle + log path in `board-orchestrator-state.json`.

**Follow-up (mandatory)**
- Create a CRITICAL root-cause ticket (or expand the existing one) to ensure:
  - starting critical → WIP also starts/records worker
  - any WIP task without worker handle auto-spawns or auto-pauses deterministically

Live examples to reference
- #56 (critical started but no worker handle)
- #34 (WIP missing worker handle)

---

## 3) WIP task missing worker handle (non-critical)
**Symptom**
- drift: `WIP #X has no worker handle recorded`

**Immediate options**
- If critical is active: pause it (or mark `paused:critical`) and proceed with critical.
- If no critical: either
  - start a worker and record handle, or
  - move to Paused/Blocked with clear reason (prefer deterministic).

**Follow-up**
- Root-cause ticket: ensure orchestrator always records worker handles when moving Ready→WIP, and can reconcile when state file is missing entries.

---

## 4) Task auto-blocked: “No repo mapping”
**Symptom**
- Ready/Backlog card moved to Blocked (auto) with reason `blocked:repo`.

**Fix**
- Add one of:
  - `Repo: /absolute/path/to/repo` in description (preferred)
  - tag `repo:<key>` that resolves via repoMap
  - tag `no-repo` if it’s planning/meta and should not require a repo

**Follow-up**
- Ensure auto-created tasks (Docs / Break down epic) include `Repo:` or `no-repo` by default.

---

## 5) “Done but not advanced” (patch produced but board stuck)
**Symptom**
- Worker log shows completed patch + instructions, but card remains in WIP/Blocked.

**Immediate action (allowed manual intervention)**
- Move the card to Review.
- Paste worker summary into the Kanboard card.

**Follow-up (mandatory)**
- Create CRITICAL root-cause ticket:
  - orchestrator should detect a completed worker output (patch file + completion marker) and advance card automatically.

Live example
- #30

---

## 6) Critical preemption changes the “problem state” too much
**Symptom**
- Preemption moves cards into a different column, so critical ticket can’t see the true failing state.

Proposed improvement
- Keep cards in WIP but mark `paused:critical`, and redefine WIP capacity as “active WIP” (unpaused only).
- Update `critical_monitor` to allow non-critical tasks in WIP if `paused:critical`.

---

## 7) Review queue gets stale
**Symptom**
- Safety check: “Review has stale items”.

Fix
- Establish a review cadence:
  - every Review card gets a quick LLM review once per revision
  - post checklist comment back to Kanboard
  - don’t auto-mark Done

---

## 8) Where to put new scenarios
Append new entries to this file under the appropriate section, with:
- Symptom
- Immediate action
- Follow-up root-cause ticket definition
- Live example ticket(s)

