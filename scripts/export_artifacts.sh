#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/export_artifacts.sh --run-dir <path>
                            [--repo-dir <path>] [--base-ref <ref>] [--allow-no-git]
                            [--include-untracked]
                            [--task-id <id>] [--title <text>] [--description <text>]
                            [--repo-key <text>] [--run-id <text>] [--print-required-lines]

Writes:
  <run-dir>/patch.patch
  <run-dir>/kanboard-comment.md
EOF
}

run_dir=""
repo_dir=""
base_ref=""
allow_no_git="0"
include_untracked="0"
task_id=""
title=""
description=""
repo_key=""
run_id=""
print_required_lines="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir)
      run_dir="${2:-}"
      shift 2
      ;;
    --repo-dir)
      repo_dir="${2:-}"
      shift 2
      ;;
    --base-ref)
      base_ref="${2:-}"
      shift 2
      ;;
    --allow-no-git)
      allow_no_git="1"
      shift 1
      ;;
    --include-untracked)
      include_untracked="1"
      shift 1
      ;;
    --task-id)
      task_id="${2:-}"
      shift 2
      ;;
    --title)
      title="${2:-}"
      shift 2
      ;;
    --description)
      description="${2:-}"
      shift 2
      ;;
    --repo-key)
      repo_key="${2:-}"
      shift 2
      ;;
    --run-id)
      run_id="${2:-}"
      shift 2
      ;;
    --print-required-lines)
      print_required_lines="1"
      shift 1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$run_dir" ]]; then
  echo "--run-dir is required" >&2
  usage >&2
  exit 2
fi

mkdir -p "$run_dir"

patch_path="${run_dir%/}/patch.patch"
comment_path="${run_dir%/}/kanboard-comment.md"

has_git_repo="0"
if [[ -z "$repo_dir" ]]; then
  if detected_root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    repo_dir="$detected_root"
  fi
fi

git_cmd() {
  if [[ "$has_git_repo" == "1" ]]; then
    git -C "$repo_dir" "$@"
  else
    git "$@"
  fi
}

if [[ -n "$repo_dir" ]] && git -C "$repo_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  has_git_repo="1"
fi

if [[ "$has_git_repo" != "1" ]]; then
  if [[ "$allow_no_git" == "1" ]]; then
    : >"$patch_path"
    {
      echo "Done."
      echo
      echo "- Patch exported via: \`(no git repo detected; empty patch)\`"
      echo "- Repo: \`${repo_key:-"(unknown)"}\`"
      echo "- Context: Kanboard task"
      echo "- Note: script ran outside a git work tree."
    } >"$comment_path"
    if [[ "$print_required_lines" == "1" ]]; then
      echo "Patch file: \`$patch_path\`"
      echo "Kanboard comment file: \`$comment_path\`"
    else
      echo "Wrote:"
      echo "  $patch_path"
      echo "  $comment_path"
    fi
    exit 0
  fi
  echo "Not a git work tree. Run inside a repo or pass --repo-dir <path>. (Use --allow-no-git to force empty outputs.)" >&2
  exit 2
fi

has_head="0"
if git_cmd rev-parse --verify HEAD >/dev/null 2>&1; then
  has_head="1"
fi

worktree_clean="1"
if [[ -n "$(git_cmd status --porcelain)" ]]; then
  worktree_clean="0"
fi

changed_files=""
untracked_files="$(git_cmd ls-files --others --exclude-standard || true)"
untracked_count="0"
if [[ -n "$untracked_files" ]]; then
  untracked_count="$(printf '%s\n' "$untracked_files" | wc -l | tr -d ' ')"
fi

append_untracked_to_patch() {
  local files="$1"
  [[ -z "$files" ]] && return 0

  local file
  while IFS= read -r file; do
    [[ -z "$file" ]] && continue
    # Use --no-index so untracked files can be included in a patch export.
    # This produces a regular diff (not a format-patch email).
    git_cmd diff --no-index -- /dev/null "$file" >>"$patch_path" || true
  done <<<"$files"
}

if [[ -n "$base_ref" ]]; then
  if [[ "$has_head" != "1" ]]; then
    echo "--base-ref requires an existing HEAD" >&2
    exit 2
  fi
  if ! git_cmd rev-parse --verify "$base_ref^{commit}" >/dev/null 2>&1; then
    echo "--base-ref '$base_ref' is not a valid commit-ish in repo: $repo_dir" >&2
    exit 2
  fi

  if [[ "$worktree_clean" == "1" ]]; then
    if [[ "$(git_cmd rev-parse "$base_ref")" == "$(git_cmd rev-parse HEAD)" ]]; then
      : >"$patch_path"
      patch_kind="(no changes since $base_ref; empty patch)"
      changed_files=""
    else
      git_cmd format-patch --stdout "${base_ref}..HEAD" >"$patch_path"
      patch_kind="git format-patch --stdout ${base_ref}..HEAD"
      changed_files="$(git_cmd diff --name-only "${base_ref}..HEAD" || true)"
    fi
  else
    git_cmd diff "$base_ref" >"$patch_path"
    patch_kind="git diff $base_ref"
    changed_files="$(git_cmd diff --name-only "$base_ref" || true)"
  fi
else
  if [[ "$has_head" == "1" ]] && [[ "$worktree_clean" == "1" ]]; then
    git_cmd format-patch -1 HEAD --stdout >"$patch_path"
    patch_kind="git format-patch -1 HEAD"
    changed_files="$(git_cmd diff-tree --no-commit-id --name-only -r HEAD || true)"
  else
    if [[ "$has_head" == "1" ]]; then
      git_cmd diff HEAD >"$patch_path"
      patch_kind="git diff HEAD"
      changed_files="$(git_cmd diff --name-only HEAD || true)"
    else
      : >"$patch_path"
      patch_kind="(no HEAD; empty patch)"
      changed_files=""
    fi
  fi
fi

if [[ -n "$untracked_files" ]]; then
  if [[ "$include_untracked" == "1" ]]; then
    append_untracked_to_patch "$untracked_files"
    patch_kind="${patch_kind} + untracked (git diff --no-index)"
    if [[ -n "$changed_files" ]]; then
      changed_files="${changed_files}"$'\n'"$untracked_files"
    else
      changed_files="$untracked_files"
    fi
  fi
fi

task_label="task"
if [[ -n "$task_id" ]]; then
  task_label="task #$task_id"
fi

if [[ -z "$repo_key" ]]; then
  if repo_root="$(git_cmd rev-parse --show-toplevel 2>/dev/null)"; then
    repo_key="$(basename "$repo_root")"
  else
    repo_key="(unknown)"
  fi
fi

{
  echo "Done."
  echo
  echo "- Patch exported via: \`$patch_kind\`"
  echo "- Repo: \`$repo_key\`"
  echo "- Context: Kanboard $task_label"
  if [[ -n "$run_id" ]]; then
    echo "- Run: \`$run_id\`"
  fi
  if [[ -n "$base_ref" ]]; then
    echo "- Base ref: \`$base_ref\`"
  fi
  if [[ -z "$title" && -z "$description" ]]; then
    echo "- Note: worker context had an empty title/description."
  else
    if [[ -n "$title" ]]; then
      echo "- Title: $title"
    fi
    if [[ -n "$description" ]]; then
      echo "- Description: (provided in worker context)"
    fi
  fi
  if [[ "$untracked_count" != "0" ]] && [[ "$include_untracked" != "1" ]]; then
    echo "- Note: $untracked_count untracked file(s) present; rerun with \`--include-untracked\` to include them in \`patch.patch\`."
    echo
    echo "Untracked files (not included in patch):"
    echo '```'
    max_untracked_list="20"
    echo "$untracked_files" | head -n "$max_untracked_list"
    if [[ "$untracked_count" -gt "$max_untracked_list" ]]; then
      echo "... (showing first $max_untracked_list of $untracked_count)"
    fi
    echo '```'
  fi
  if [[ -n "$changed_files" ]]; then
    echo
    echo "Files changed:"
    echo '```'
    echo "$changed_files"
    echo '```'
  fi
} >"$comment_path"

if [[ "$print_required_lines" == "1" ]]; then
  echo "Patch file: \`$patch_path\`"
  echo "Kanboard comment file: \`$comment_path\`"
else
  echo "Wrote:"
  echo "  $patch_path"
  echo "  $comment_path"
fi
