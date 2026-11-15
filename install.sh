#!/usr/bin/env sh
# Install pile-digest as a personal Claude Code skill (symlink, so `git pull` updates it).
#   ./install.sh             install (symlink into ~/.claude/skills/pile-digest)
#   ./install.sh --copy      install by copying instead of symlinking
#   ./install.sh --uninstall remove it (your piles' state in ~/.local/state/pile-digest is kept)
set -eu
SRC="$(cd "$(dirname "$0")" && pwd)/skills/pile-digest"
DEST_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
DEST="$DEST_DIR/pile-digest"

case "${1:-}" in
  --uninstall)
    if [ -L "$DEST" ] || [ -d "$DEST" ]; then rm -rf "$DEST"; echo "Removed $DEST"; else echo "Not installed."; fi
    exit 0 ;;
esac

command -v python3 >/dev/null 2>&1 || { echo "error: python3 (3.9+) is required" >&2; exit 1; }
mkdir -p "$DEST_DIR"
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  echo "Replacing existing $DEST"
  rm -rf "$DEST"
fi
if [ "${1:-}" = "--copy" ]; then cp -R "$SRC" "$DEST"; else ln -s "$SRC" "$DEST"; fi
echo "Installed to $DEST"
echo
python3 "$DEST/scripts/pd.py" doctor || true
echo
echo "In Claude Code, type: /pile-digest <folder>"
