#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script_path="$root_dir/scripts/export_artifacts.sh"

if [[ ! -x "$script_path" ]]; then
  echo "Expected executable: $script_path" >&2
  exit 1
fi

tmp_dir="$(mktemp -d)"
cleanup() { rm -rf "$tmp_dir"; }
trap cleanup EXIT

repo_dir="$tmp_dir/repo"
run_dir="$tmp_dir/run"
mkdir -p "$repo_dir" "$run_dir"

git -C "$repo_dir" init -q
git -C "$repo_dir" config user.email "smoke@test.invalid"
git -C "$repo_dir" config user.name "Smoke Test"

echo "hello" >"$repo_dir/hello.txt"
git -C "$repo_dir" add hello.txt
git -C "$repo_dir" commit -qm "init"

rm -rf "$run_dir" && mkdir -p "$run_dir"
bash "$script_path" --repo-dir "$repo_dir" --run-dir "$run_dir" --task-id 0 --title "" --description "" >/dev/null

grep -q "^Subject: " "$run_dir/patch.patch"

echo "untracked" >"$repo_dir/untracked.txt"

rm -rf "$run_dir" && mkdir -p "$run_dir"
bash "$script_path" --repo-dir "$repo_dir" --run-dir "$run_dir" >/dev/null

if [[ -s "$run_dir/patch.patch" ]]; then
  echo "Expected empty patch when only untracked files exist (without --include-untracked)" >&2
  exit 1
fi
grep -q "untracked file(s) present" "$run_dir/kanboard-comment.md"
grep -q "Untracked files (not included in patch)" "$run_dir/kanboard-comment.md"
grep -q "^untracked.txt$" "$run_dir/kanboard-comment.md"

rm -rf "$run_dir" && mkdir -p "$run_dir"
bash "$script_path" --repo-dir "$repo_dir" --run-dir "$run_dir" --include-untracked >/dev/null

grep -q "^diff --git a/untracked.txt b/untracked.txt" "$run_dir/patch.patch"

echo "OK"
