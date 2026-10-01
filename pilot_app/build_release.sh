#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Which Python? The version string is read out of the tree itself, so this needs
# an interpreter that can import pilot_app -- and the line used to name
# "$ROOT_DIR/.venv-pilot/bin/python", a path that exists on exactly one laptop.
# Anywhere else (a CI runner, a contributor's checkout, /opt on the server) the
# build died on line 5 before doing anything at all. Ask for a usable
# interpreter instead of assuming one.
find_python() {
  local candidate
  for candidate in "${PYTHON:-}" \
                   "$ROOT_DIR/.venv-pilot/bin/python" \
                   "$ROOT_DIR/.venv/bin/python" \
                   python3 python; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 &&
       PYTHONPATH="$ROOT_DIR" "$candidate" -c 'import pilot_app' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  echo "找不到能 import pilot_app 的 Python（试过 \${PYTHON}、.venv-pilot、.venv、python3、python）。" >&2
  return 1
}

PY="$(find_python)"
VERSION="$(PYTHONPATH="$ROOT_DIR" "$PY" -c 'from pilot_app import __version__; print(__version__)')"
ARCHIVE="cityu-mail-pilot-$VERSION.tar.gz"

# LICENSE ships with the runtime package: the installer copies it next to the
# code it just installed, which is what AGPL expects when you hand somebody the
# software. A root README is included when present so a freshly unpacked release
# explains itself. (The first version of this script omitted both, and the
# installer died halfway through an upgrade on the missing file.)
EXTRA=()
[[ -f "$ROOT_DIR/LICENSE" ]] && EXTRA+=(LICENSE)
[[ -f "$ROOT_DIR/README.md" ]] && EXTRA+=(README.md)

# Tests of the *developer tooling* cannot run from this package, because the
# package is the runtime: it ships `pilot_app/` and nothing else. They import
# `tools/` or read `.github/`, neither of which is here, so shipping them means
# shipping a suite that fails the moment somebody unpacks it -- which is a lie
# in artifact form. The release job of the first CI run found exactly that: six
# import errors, and a package whose own tests could not pass.
#
# The list is not trusted to stay complete by hand: `ReleasePackageTests` in
# pilot_app/tests/test_ci.py re-derives it from the tree and fails if a test that
# needs repo-level files is missing here.
#
# The three `*_shots` modules were the ones the derivation could not see: they
# read their generator through a plain path join (`ROOT / "tools" / "x.js"`)
# rather than an import, so they shipped inside the package and failed there.
# That is what kept CI's 「发布包能装也能跑」 job red for several pushes
# (2026-09-18) while every local run was green -- the package is the one tree
# nobody tests by hand.
REPO_ONLY_TESTS=(
  test_appcode_shots.py
  test_check_master_key.py
  test_ci.py
  test_cleanup_local.py
  test_dependency_audit.py
  test_forward_shots.py
  test_handoff.py
  test_task_flow_integration.py
  test_i18n_proofread.py
  test_install_shots.py
  test_installer.py
  test_notify_stalled.py
  test_preflight.py
  test_pr_gate.py
  test_pr_triage.py
  test_publish_export.py
  test_python_targets.py
  test_model_resources.py
  test_shell_scripts.py
)
REPO_ONLY_EXCLUDES=()
for _name in "${REPO_ONLY_TESTS[@]}"; do
  REPO_ONLY_EXCLUDES+=("--exclude=pilot_app/tests/$_name")
done

mkdir -p "$ROOT_DIR/dist"
# macOS tar writes each file's extended attributes into the archive as an
# AppleDouble `._name` member. bsdtar hides those when listing, so the archive
# looked clean here and unpacked as 52 junk files plus a screen of warnings for
# anyone on Linux -- most of a third of the release was metadata. COPYFILE_DISABLE
# is the supported switch and is a no-op on GNU tar; the --exclude lines are
# belt-and-braces so a stray .DS_Store from Finder cannot ride along either.
# `*.bak-pr*`（2026-09-24 加）：`pr_triage.py --apply` 会把每个被改的文件备份成
# `<名字>.bak-prN` **放在树里**（它自己会打印出来，供回退用）。那是临时物，而且内容
# 比正式文件旧 —— 不该跟着发布包上服务器。
COPYFILE_DISABLE=1 tar \
  --no-xattrs \
  --exclude='pilot_app/__pycache__' \
  --exclude='pilot_app/tests/__pycache__' \
  --exclude='pilot_app/.env' \
  --exclude='pilot_app/*.sqlite3' \
  --exclude='*.bak-pr*' \
  --exclude='._*' \
  --exclude='*/._*' \
  --exclude='.DS_Store' \
  --exclude='*/.DS_Store' \
  "${REPO_ONLY_EXCLUDES[@]}" \
  -czf "$ROOT_DIR/dist/$ARCHIVE" \
  -C "$ROOT_DIR" pilot_app "${EXTRA[@]:-}"
(
  cd "$ROOT_DIR/dist"
  # `shasum` is a Perl script macOS ships; Linux has `sha256sum` from coreutils.
  # Naming only one of them is the same bug as the hard-coded interpreter above,
  # in a smaller place. The two write the same file format, so `-c` works either way.
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"
  else
    shasum -a 256 "$ARCHIVE" > "$ARCHIVE.sha256"
  fi
)
echo "$ROOT_DIR/dist/$ARCHIVE"
echo "校验方式：cd $ROOT_DIR/dist && shasum -a 256 -c $ARCHIVE.sha256（Linux 上用 sha256sum -c）"
