#!/usr/bin/env bash
# Standalone translation pipeline: extract → update → compile → copy → verify.
# Self-contained: no .development/ dependency, no plugin-config.json.
# Used by .githooks/check_translations_sync.sh and CI.
#
# Usage: compile_translations.sh
#   Runs the default pipeline (extract, update, compile, copy to package).
#   Set FORCE_CLEAN=true to skip interactive prompts for obsolete-entry cleanup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PLUGIN_PACKAGE="octoprint_pandabreath"
TRANSLATIONS_DIR="$PLUGIN_PACKAGE/translations"
BABEL_CFG="$REPO_ROOT/babel.cfg"

# Find pybabel: prefer .venv, fall back to PATH
if [[ -x ".venv/bin/pybabel" ]]; then
    PYBABEL=".venv/bin/pybabel"
elif command -v pybabel >/dev/null 2>&1; then
    PYBABEL="pybabel"
else
    echo "ERROR: pybabel not found. Install Babel: pip install Babel" >&2
    exit 1
fi

# Find python3: prefer .venv
if [[ -x ".venv/bin/python3" ]]; then
    PYTHON=".venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
else
    PYTHON=""
fi

# --- Extract ---
echo "Extracting translatable strings → translations/messages.pot ..."
"$PYBABEL" extract --sort-output -F "$BABEL_CFG" \
    --ignore-dirs 'build dist *.egg-info node_modules .venv .tox .git' \
    -o translations/messages.pot \
    setup.py "$PLUGIN_PACKAGE" tests

# --- Stabilize POT-Creation-Date before update (prevent parse errors + timestamp churn) ---
# pybabel extract uses a placeholder "YEAR-MO-DA HO:MI+ZONE" when the existing
# POT had that default; pybabel update then fails to parse it. Fix by setting a
# deterministic value before running update.
if [[ -n "$PYTHON" ]]; then
    "$PYTHON" - <<'PY' || true
import sys
from pathlib import Path
try:
    import polib
except ImportError:
    sys.exit(0)
STABLE = "1970-01-01 00:00+0000"
pot = Path("translations/messages.pot")
if pot.exists():
    try:
        cat = polib.pofile(str(pot))
        val = cat.metadata.get("POT-Creation-Date", "")
        if not val or "YEAR" in val or "MO" in val:
            cat.metadata["POT-Creation-Date"] = STABLE
        cat.save()
    except Exception:
        pass
PY
fi

# --- Update PO files ---
echo "Updating PO files from POT ..."
"$PYBABEL" update --no-fuzzy-matching -i translations/messages.pot -d translations

# --- Clean obsolete entries ---
echo "Removing obsolete entries ..."
if [[ -n "$PYTHON" ]] && command -v python3 >/dev/null 2>&1; then
    "$PYTHON" - <<'PY' || true
import sys
from pathlib import Path
try:
    import polib
except ImportError:
    sys.exit(0)
for path in sorted(Path("translations").rglob("*.po")):
    try:
        cat = polib.pofile(str(path))
        cat[:] = [e for e in cat if not e.obsolete]
        cat.save()
    except Exception:
        pass
PY
elif command -v msgattrib >/dev/null 2>&1; then
    shopt -s nullglob
    for po in translations/*/LC_MESSAGES/*.po; do
        msgattrib --no-obsolete "$po" > "${po}.clean" && mv "${po}.clean" "$po"
    done
    shopt -u nullglob
fi

# --- Compile top-level catalogs ---
echo "Compiling top-level catalogs ..."
if command -v msgfmt >/dev/null 2>&1; then
    shopt -s nullglob
    for po in translations/*/LC_MESSAGES/*.po; do
        msgfmt -o "${po%.po}.mo" "$po"
    done
    shopt -u nullglob
else
    "$PYBABEL" compile -d translations
fi

# --- Copy compiled MO files into package ---
echo "Copying .mo files → $TRANSLATIONS_DIR/ ..."
shopt -s nullglob
for mo in translations/*/LC_MESSAGES/*.mo; do
    lang="$(basename "$(dirname "$(dirname "$mo")")")"
    target="$TRANSLATIONS_DIR/$lang/LC_MESSAGES"
    mkdir -p "$target"
    cp -a "$mo" "$target/"
done
shopt -u nullglob

# --- Stabilize all POT-Creation-Date headers after update (prevent timestamp churn) ---
if [[ -n "$PYTHON" ]]; then
    "$PYTHON" - <<'PY' || true
import sys
from pathlib import Path
try:
    import polib
except ImportError:
    sys.exit(0)
STABLE = "1970-01-01 00:00+0000"
for path in list(Path("translations").rglob("*.po")) + list(Path("translations").rglob("*.pot")):
    try:
        cat = polib.pofile(str(path))
        cat.metadata["POT-Creation-Date"] = STABLE
        cat.save()
    except Exception:
        pass
PY
fi

# --- Verify catalogs ---
if [[ -n "$PYTHON" ]]; then
    "$PYTHON" - <<'PY'
import sys
from pathlib import Path
try:
    import polib
except ImportError:
    print("polib not available; skipping verification.", file=sys.stderr)
    sys.exit(0)
bad = []
for po_path in sorted(Path("translations").glob("*/LC_MESSAGES/messages.po")):
    lang = po_path.parts[1]
    try:
        po = polib.pofile(str(po_path))
    except Exception as e:
        bad.append(f"{po_path}: cannot parse ({e})")
        continue
    for entry in po:
        if entry.obsolete:
            continue
        if "fuzzy" in entry.flags and entry.msgstr and entry.msgstr != entry.msgid:
            bad.append(f"{lang}: fuzzy mismatch {entry.msgid!r} → {entry.msgstr!r}")
        if lang == "en" and entry.msgstr and entry.msgstr != entry.msgid:
            bad.append(f"en: msgstr differs from msgid for {entry.msgid!r}")
if bad:
    print("Catalog verification FAILED:", file=sys.stderr)
    for b in bad:
        print(f"  - {b}", file=sys.stderr)
    sys.exit(1)
print("Catalogs OK.")
PY
fi

echo "Done."
