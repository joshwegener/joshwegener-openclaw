# clawd scripts

## `export_artifacts.sh`

Helper to write the two run artifacts expected by RecallDeck worker runs:

- `patch.patch` (prefers `git format-patch -1 HEAD` when the work tree is clean; otherwise uses `git diff HEAD`)
- `kanboard-comment.md` (ready to paste into Kanboard)

Usage:

```bash
bash scripts/export_artifacts.sh --run-dir /path/to/run-dir --task-id 47 --title "..." --description "..."
```

Optional flags:

- `--repo-dir <path>`: run git commands in a specific repo (default: auto-detect current repo)
- `--base-ref <ref>`: export changes since a base commit-ish (committed => `format-patch`, dirty => `diff`)
- `--allow-no-git`: allow running outside a git work tree (writes an empty patch + comment)
- `--include-untracked`: include untracked (non-ignored) files in `patch.patch` using `git diff --no-index`
- `--repo-key <text>`: override the repo key shown in the Kanboard comment (defaults to the git repo folder name)
- `--run-id <text>`: include the worker `run_id` in the Kanboard comment
- `--print-required-lines`: print exactly the two lines required by the worker harness (paths to `patch.patch` and `kanboard-comment.md`)
