#!/usr/bin/env bash
# Find the submission link on a directory's homepage.
#
# Tries common submission URL paths first (fast, no browser), then falls back
# to scanning the homepage for likely "Submit" / "Add Tool" / "List Your Tool"
# links via agent-browser.
#
# Usage: find_submit_link.sh <homepage-url> [session-name]
# Prints the discovered submission URL on stdout, or exits 1 if none found.

set -euo pipefail

HOMEPAGE="${1:?Usage: $0 <homepage-url> [session-name]}"
SESSION="${2:-submitdir}"

# Strip trailing slash for clean joining
BASE="${HOMEPAGE%/}"

# Common direct paths used by AI directories
CANDIDATES=(
  "$BASE/submit"
  "$BASE/submit-tool"
  "$BASE/submit-your-tool"
  "$BASE/submit-your-ai-tool"
  "$BASE/submit-ai-tool"
  "$BASE/submit-ai"
  "$BASE/add"
  "$BASE/add-tool"
  "$BASE/add-ai-tool"
  "$BASE/new"
  "$BASE/post"
  "$BASE/list-your-tool"
  "$BASE/list-tool"
)

# Probe direct candidates with curl (HEAD); first 2xx wins.
for url in "${CANDIDATES[@]}"; do
  status=$(curl -s -o /dev/null -L -w "%{http_code}" --max-time 8 -A "Mozilla/5.0 submit-bot" "$url" || echo "000")
  if [[ "$status" =~ ^2 ]]; then
    echo "$url"
    exit 0
  fi
done

# Fallback: load the homepage and look for likely submit-link text.
agent-browser --session "$SESSION" open "$HOMEPAGE" >/dev/null
agent-browser --session "$SESSION" wait --load networkidle >/dev/null 2>&1 || true

# Try a JSON list of labels in one pass — avoids brittle per-shell-loop quoting.
# Compatible with bash 3.2 (macOS default); no ${var@Q} bashism.
LABELS_JSON='["Submit tool","Submit Tool","Submit a tool","Submit AI","Submit your tool","Submit Your Tool","Submit","Add Tool","Add a tool","Add AI Tool","List Your Tool","List your tool","List Tool","Post a tool","Add"]'

url=$(agent-browser --session "$SESSION" eval --stdin 2>/dev/null <<EVALEOF || true
(() => {
  const labels = ${LABELS_JSON};
  const links = Array.from(document.querySelectorAll('a'));
  for (const label of labels) {
    const want = label.toLowerCase();
    const hit = links.find(a => (a.textContent || '').trim().toLowerCase() === want)
             || links.find(a => (a.textContent || '').trim().toLowerCase().includes(want));
    if (hit && hit.href) return hit.href;
  }
  return '';
})()
EVALEOF
)
# eval may wrap in quotes; strip them
url=$(printf '%s' "$url" | tr -d '"' | tr -d "'")
if [ -n "$url" ] && [ "$url" != "null" ] && [ "$url" != "undefined" ]; then
  echo "$url"
  exit 0
fi

exit 1
