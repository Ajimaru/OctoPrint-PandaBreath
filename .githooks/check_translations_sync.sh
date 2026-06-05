#!/usr/bin/env bash

# Helper: verify that top-level PO catalogs are synchronized with translations/messages.pot
# Behavior:
#  - Creates a temporary copy of `translations/`, runs `pybabel update` against the POT
#    on the temporary copy and compares it to the real `translations/` directory.
#  - Performs a read-only check (does not modify the working tree); intended for pre-commit.
#  - Exits 0 when translations are up-to-date; otherwise prints diffs and exits non-zero.
# Usage:
#  - Typically invoked from a pre-commit hook or CI to ensure translators are synchronized.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Prefer the git top-level so the hook works regardless of whether it runs
# from .githooks (repo root) or .development/.githooks (one level deeper).
if ! REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
    REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
cd "$REPO_ROOT"

VENV_PYBABEL="./.venv/bin/pybabel"
VENV_PYTHON="./.venv/bin/python3"

PYBABEL=""
if [[ -x "$VENV_PYBABEL" ]]; then
  PYBABEL="$VENV_PYBABEL"
else
  if command -v pybabel >/dev/null 2>&1; then
    PYBABEL="$(command -v pybabel)"
  else
    echo "pybabel not found in ./.venv or system PATH. Install dev requirements: pip install Babel" >&2
    exit 1
  fi
fi

if [[ ! -f translations/messages.pot ]]; then
  echo "POT file not found at translations/messages.pot. Update .pot files first." >&2
  exit 1
fi

echo "Checking translations against POT (no changes will be made to working tree)..."

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

# copy translations to temporary location
cp -a translations "$tmpdir"/translations

# run pybabel update on the temporary copy
if ! output="$("$PYBABEL" update -i translations/messages.pot -d "$tmpdir"/translations 2>&1)"; then
    echo "pybabel failed while updating temporary translations. Output follows:" >&2
    printf '%s\n' "$output" >&2
    exit 1
fi

# normalize and compare using Python with polib for canonical representation
if [[ -x "$VENV_PYTHON" ]]; then
  "$VENV_PYTHON" - <<'COMPARE' "$REPO_ROOT" "$tmpdir"
import sys
from pathlib import Path

def normalize_po_content(path: str) -> str:
    """Normalize a PO/POT file using polib for canonical representation."""
    if not Path(path).exists():
        return ""

    try:
        import polib
        pofile = polib.pofile(path)
        return pofile.__unicode__()
    except Exception:
        # Fallback to filtering timestamps manually
        text = Path(path).read_text(encoding="utf-8")
        lines = text.splitlines()
        out = []
        for line in lines:
            if line.startswith('"POT-Creation-Date:'):
                continue
            if line.startswith('"Generated-By:'):
                continue
            out.append(line)
        return "\n".join(out).strip() + "\n"

repo_root = Path(sys.argv[1])
tmpdir = Path(sys.argv[2])
real_dir = repo_root / "translations"
temp_dir = tmpdir / "translations"

if not real_dir.exists() or not temp_dir.exists():
    print("ERROR: translations directory not found", file=sys.stderr)
    sys.exit(1)

# Get all relative paths for .pot and .po files in both directories
real_po_files = {f.relative_to(real_dir) for f in real_dir.rglob("*") if f.suffix in {".pot", ".po"} and f.is_file()}
temp_po_files = {f.relative_to(temp_dir) for f in temp_dir.rglob("*") if f.suffix in {".pot", ".po"} and f.is_file()}

all_relative_paths = real_po_files | temp_po_files
differences = []

for rel_path in sorted(all_relative_paths):
    real_file = real_dir / rel_path
    temp_file = temp_dir / rel_path

    real_normalized = normalize_po_content(str(real_file))
    temp_normalized = normalize_po_content(str(temp_file))

    if real_normalized != temp_normalized:
        differences.append(str(rel_path))

if differences:
    print("ERROR: Translations are out of sync with translations/messages.pot.", file=sys.stderr)
    print("Files with content differences:", file=sys.stderr)
    for f in differences:
        print(f"  {f}", file=sys.stderr)
    sys.exit(1)
else:
    print("Translations are up-to-date.")
    sys.exit(0)
COMPARE
  exit_code=$?
  exit "$exit_code"
fi

printf '%s\n' "ERROR: Unable to run Python for comparison check"
exit 1
