# HEARTBEAT.md

# Minimal heartbeat tasks.
# This file is intentionally small. If nothing is urgent, reply HEARTBEAT_OK.

- If a scheduled cron/system event is waiting (RecallDeck hourly update, email summaries), process it and send the update.
- If RecallDeck tools are available and init has not run this session: call `init_task` (or `init`), then execute `next_calls` before doing anything else.
- Otherwise: HEARTBEAT_OK.
