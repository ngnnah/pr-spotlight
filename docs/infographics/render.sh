#!/bin/sh
# Renders each infographic page to docs/img/<name>.png, 1000 CSS px wide at 2x, as tall as its content.
# Usage: docs/infographics/render.sh [name ...]   (default: every .html here)
cd "$(dirname "$0")" || exit 1
chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[ $# -eq 0 ] && set -- $(ls *.html | sed 's/\.html$//')
for name in "$@"; do
  url="file://$PWD/$name.html"
  h=$("$chrome" --headless=new --window-size=1000,400 --dump-dom "$url" 2>/dev/null |
      sed -n 's/.*data-h="\([0-9]*\)".*/\1/p' | head -1)
  "$chrome" --headless=new --hide-scrollbars --force-device-scale-factor=2 \
    --window-size=1000,"${h:-800}" --screenshot="$PWD/../img/$name.png" "$url" 2>/dev/null
  echo "docs/img/$name.png (${h:-800} px tall)"
done
