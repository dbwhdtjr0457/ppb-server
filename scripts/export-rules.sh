#!/bin/bash
# Snapshot a matching client build. No Git upload, images or save data are sent.
set -euo pipefail
if [[ $# != 2 ]]; then
  echo 'Usage: bash scripts/export-rules.sh /path/to/PPB/source /new/destination' >&2
  exit 1
fi
SOURCE=$(cd "$1" && pwd)
DESTINATION="$2"
if [[ -e "$DESTINATION" ]]; then
  echo 'Destination already exists; choose a new directory to preserve the old engine.' >&2
  exit 1
fi
(cd "$SOURCE" && swift build -c release)
mkdir -p "$DESTINATION"
cp "$SOURCE/.build/release/PokePackBar" "$DESTINATION/PokePackBar"
cp -R "$SOURCE/.build/release/PokePackBar_PokePackBar.bundle" "$DESTINATION/"
mkdir -p "$DESTINATION/price-tools"
cp "$SOURCE/scripts/update_printing_prices.py" "$SOURCE/scripts/update_pack_prices.py" "$SOURCE/scripts/curated-price-references.json" "$DESTINATION/price-tools/"
echo "Exported rules and matching resources to $DESTINATION"
