#!/usr/bin/env bash
# Submit a website to all AI directories from best-of-ai.
# Designed to run unattended: every per-directory failure is caught and recorded.
#
# Usage: run_submit_batch.sh <workspace-dir> [limit] [start-from]
#   workspace-dir : contains submission-info.json; gets shots/ + history + report
#   limit         : max directories to process (default: all)
#   start-from    : skip directories before this 1-indexed position (default: 1)

set -u  # but not -e — we want to continue on per-directory failures

WS="${1:?usage: $0 <workspace-dir> [limit] [start-from]}"
LIMIT="${2:-0}"
START="${3:-1}"

SKILL=/Users/max/.claude/skills/submit-to-ai-directories
SESSION=aibanana

# Cap how long any single agent-browser call can hang on a slow page.
# Without this, a stuck field-fill on one directory wedges the whole batch.
export AGENT_BROWSER_DEFAULT_TIMEOUT=12000

# URL patterns that mean "this directory requires payment" — never submit.
PAID_RE='stripe\.com|checkout\.|gumroad\.com|lemonsqueezy\.com|paddle\.com|paypal\.com|/upgrade|/pricing|/plan|/billing'

LIST="$WS/dirs.json"
HISTORY="$WS/submission-history.json"
LOG="$WS/run.log"
INFO="$WS/submission-info.json"
SHOTS="$WS/shots"

mkdir -p "$SHOTS"

# Load info
TOOL_URL=$(jq -r .url "$INFO")
TOOL_NAME=$(jq -r .name "$INFO")
EMAIL=$(jq -r .email "$INFO")
TAGLINE=$(jq -r .tagline "$INFO")
SHORT_DESC=$(jq -r .short_description "$INFO")
LONG_DESC=$(jq -r .long_description "$INFO")
CATEGORY=$(jq -r .categories[0] "$INFO")

# Init
[ -f "$HISTORY" ] || echo "[]" > "$HISTORY"
[ -f "$LIST" ] || python3 "$SKILL/scripts/fetch_directories.py" --format json > "$LIST"

# HEADED=1 shows the browser window (useful for watching, debugging, manual login).
# Default unset = headless (faster, but invisible).
HEADED="${HEADED:-0}"

# When INTERACTIVE=1 and a login wall is hit, the script pauses and waits for
# stdin so the user can manually log in (e.g. "Sign in with Google") in the
# headed browser window. Press Enter after logging in to continue.
# Only useful with HEADED=1 and a terminal stdin.
INTERACTIVE="${INTERACTIVE:-0}"

# Optional: load a saved auth state file at start (cookies/localStorage from a
# previous session). Lets a one-time interactive login persist across batches.
#   agent-browser --session $SESSION state save auth.json   # one-time, after login
#   STATE_FILE=auth.json bash run_batch.sh ...               # subsequent runs
STATE_FILE="${STATE_FILE:-}"

# SCOUT_ONLY=1 means: walk the list, find each directory's submit URL, classify
# walls (login / paid / captcha / no-form / iframe-embed), but DO NOT fill or
# submit any forms. Use this to triage a big list before manual submission.
# In scout mode, directories with a real form that don't hit a wall are recorded
# as `skipped` with reason `needs-manual-submission` — retryable by design.
SCOUT_ONLY="${SCOUT_ONLY:-0}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" | tee -a "$LOG" >&2; }

append_history() {
  local entry="$1"
  local tmp
  tmp=$(mktemp)
  jq --argjson e "$entry" '. += [$e]' "$HISTORY" > "$tmp" && mv "$tmp" "$HISTORY"
}

# agent-browser wrapper. The --headed flag and state load happen on the
# first call (via the session's open command); after that the daemon
# keeps the window visible for all subsequent calls.
_AB_HEADED_FLAG=""
[ "$HEADED" = "1" ] && _AB_HEADED_FLAG="--headed"

ab() {
  agent-browser $_AB_HEADED_FLAG --session "$SESSION" "$@" 2>/dev/null
}

# Load saved auth state if provided (do this before any open call)
if [ -n "$STATE_FILE" ] && [ -f "$STATE_FILE" ]; then
  log "loading auth state from $STATE_FILE"
  ab state load "$STATE_FILE" >/dev/null || true
fi

try_fill() {
  # Best-effort: try each candidate label until one works.
  local value="$1"; shift
  for label in "$@"; do
    if ab find label "$label" fill "$value" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

submit_one() {
  local dir_name="$1" dir_url="$2"
  local status="unknown" reason="" submit_url="" final_url="" shot=""
  local now safe_name
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  safe_name=$(echo "$dir_name" | tr -c 'a-zA-Z0-9' '_' | tr -s _ | sed 's/^_\|_$//g')

  log "→ $dir_name ($dir_url)"

  # 3a. Dedup
  if python3 "$SKILL/scripts/check_submitted.py" "$HISTORY" "$TOOL_URL" "$dir_url" >/dev/null 2>&1; then
    log "  ⊘ already submitted"
    return
  fi

  # 3b. Resolve submit URL
  submit_url=$(bash "$SKILL/scripts/find_submit_link.sh" "$dir_url" "$SESSION" 2>/dev/null || true)
  if [ -z "$submit_url" ]; then
    log "  ⊘ no submit link"
    append_history "$(jq -nc --arg tu "$TOOL_URL" --arg tn "$TOOL_NAME" --arg dn "$dir_name" --arg du "$dir_url" --arg at "$now" \
      '{tool_url:$tu, tool_name:$tn, directory_name:$dn, directory_url:$du, submit_url:"", status:"skipped", reason:"no submit form found", screenshot:"", final_url:"", submitted_at:$at}')"
    return
  fi

  # 3c. Open and switch to iframe form if present
  ab open "$submit_url" >/dev/null || true
  ab wait --load networkidle >/dev/null || true

  local current_url
  current_url=$(ab get url 2>/dev/null || echo "$submit_url")

  # Detect login wall
  if echo "$current_url" | grep -qiE 'login|signin|auth/'; then
    if [ "$INTERACTIVE" = "1" ] && [ -t 0 ]; then
      # Interactive mode: pause so user can log in manually (e.g. Sign in with Google),
      # then continue the form-fill flow on the page they reach.
      log "  ⏸  login wall — sign in in the browser window, then press Enter here"
      printf "    Browser is at: %s\n" "$current_url" >&2
      printf "    Press Enter once logged in (or Ctrl+C to abort): " >&2
      read -r _ || true
      # Re-check where we are
      current_url=$(ab get url 2>/dev/null || echo "$current_url")
      log "  ▶ resuming at $current_url"
    else
      log "  ⊘ login-required ($current_url)"
      append_history "$(jq -nc --arg tu "$TOOL_URL" --arg tn "$TOOL_NAME" --arg dn "$dir_name" --arg du "$dir_url" --arg su "$submit_url" --arg fu "$current_url" --arg at "$now" \
        '{tool_url:$tu, tool_name:$tn, directory_name:$dn, directory_url:$du, submit_url:$su, status:"skipped", reason:"login-required", screenshot:"", final_url:$fu, submitted_at:$at}')"
      return
    fi
  fi

  # Detect verify/captcha wall
  if echo "$current_url" | grep -qiE 'verify|captcha|challenge'; then
    log "  ⊘ captcha-wall ($current_url)"
    append_history "$(jq -nc --arg tu "$TOOL_URL" --arg tn "$TOOL_NAME" --arg dn "$dir_name" --arg du "$dir_url" --arg su "$submit_url" --arg fu "$current_url" --arg at "$now" \
      '{tool_url:$tu, tool_name:$tn, directory_name:$dn, directory_url:$du, submit_url:$su, status:"skipped", reason:"captcha", screenshot:"", final_url:$fu, submitted_at:$at}')"
    return
  fi

  # Detect payment redirect BEFORE filling — some directories send /submit straight to checkout.
  if echo "$current_url" | grep -qiE "$PAID_RE"; then
    log "  ⊘ paid-only ($current_url)"
    append_history "$(jq -nc --arg tu "$TOOL_URL" --arg tn "$TOOL_NAME" --arg dn "$dir_name" --arg du "$dir_url" --arg su "$submit_url" --arg fu "$current_url" --arg at "$now" \
      '{tool_url:$tu, tool_name:$tn, directory_name:$dn, directory_url:$du, submit_url:$su, status:"skipped", reason:"paid-only", screenshot:"", final_url:$fu, submitted_at:$at}')"
    return
  fi

  # Detect iframe form
  local iframe
  iframe=$(ab eval 'Array.from(document.querySelectorAll("iframe")).map(f => f.src).filter(s => /tally|typeform|fillout|paperform|airtable|google\.com\/forms/.test(s))[0] || ""' 2>/dev/null | tr -d '"' | head -1)
  if [ -n "$iframe" ] && [ "$iframe" != "null" ] && [ "$iframe" != "undefined" ]; then
    log "  ↪ iframe form: $iframe"
    ab open "$iframe" >/dev/null || true
    ab wait --load networkidle >/dev/null || true
  fi

  # Scout-only mode: capture a screenshot of the form, record as needs-manual,
  # and stop here (don't try to fill or submit).
  if [ "$SCOUT_ONLY" = "1" ]; then
    shot="shots/${safe_name}_scout.png"
    ab screenshot --full "$WS/$shot" >/dev/null || true
    local form_kind="generic"
    [ -n "$iframe" ] && [ "$iframe" != "null" ] && [ "$iframe" != "undefined" ] && form_kind="iframe-embed"
    final_url=$(ab get url 2>/dev/null || echo "$current_url")
    log "  ⊘ scout: form found ($form_kind), needs manual submission"
    append_history "$(jq -nc --arg tu "$TOOL_URL" --arg tn "$TOOL_NAME" --arg dn "$dir_name" --arg du "$dir_url" --arg su "$submit_url" --arg sh "$shot" --arg fu "$final_url" --arg rn "needs-manual-submission ($form_kind)" --arg at "$now" \
      '{tool_url:$tu, tool_name:$tn, directory_name:$dn, directory_url:$du, submit_url:$su, status:"skipped", reason:$rn, screenshot:$sh, final_url:$fu, submitted_at:$at}')"
    return
  fi

  # 3d. Fill (best-effort across common label variations — fragile, see SKILL.md)
  try_fill "$TOOL_NAME"   "Tool Name" "Product Name" "Name" "Title" "Tool Title" "Your tool name" "Tool's Name" || true
  try_fill "$TOOL_URL"    "Tool URL" "Website" "Website URL" "URL" "Tool's Link" "Link" "Product URL" || true
  try_fill "$EMAIL"       "Email" "Your Email" "Contact Email" "Your email address" || true
  try_fill "$SHORT_DESC"  "Short Description" "Description" "Tagline" "Pitch" "About" "One-liner" || true
  try_fill "$LONG_DESC"   "Long Description" "Detailed Description" "Details" "Additional Notes" "More info" || true
  try_fill "$CATEGORY"    "Category" "Tool category" "Tag" "Type" || true
  try_fill "$TOOL_NAME"   "Your Name" "Submitter Name" || true  # fallback when form asks for submitter

  # Screenshot of filled state
  shot="shots/${safe_name}.png"
  ab screenshot --full "$WS/$shot" >/dev/null || true

  # 3e. Submit
  local clicked=0
  for label in "Submit" "Submit Tool" "Submit Your Tool" "Send" "Add Tool" "Post"; do
    if ab find role button click --name "$label" >/dev/null 2>&1; then
      clicked=1; break
    fi
  done

  if [ $clicked -eq 0 ]; then
    log "  ✗ failed: submit button not found"
    append_history "$(jq -nc --arg tu "$TOOL_URL" --arg tn "$TOOL_NAME" --arg dn "$dir_name" --arg du "$dir_url" --arg su "$submit_url" --arg sh "$shot" --arg fu "$current_url" --arg at "$now" \
      '{tool_url:$tu, tool_name:$tn, directory_name:$dn, directory_url:$du, submit_url:$su, status:"failed", reason:"submit button not found", screenshot:$sh, final_url:$fu, submitted_at:$at}')"
    return
  fi

  ab wait --load networkidle >/dev/null || true
  sleep 2
  final_url=$(ab get url 2>/dev/null || echo "$current_url")

  # Capture post-submit screenshot
  local shot_after="shots/${safe_name}_after.png"
  ab screenshot --full "$WS/$shot_after" >/dev/null || true

  # Classify outcome
  if echo "$final_url" | grep -qiE "$PAID_RE"; then
    # Submit succeeded but redirected to a paywall — this directory charges to list.
    status="skipped"; reason="paid-only"
    shot="$shot_after"
  elif echo "$final_url" | grep -qiE 'thank|success|received|submitted|pending|confirm|complete'; then
    status="submitted"; reason=""
    shot="$shot_after"
  else
    # Check page text for success markers
    local page_text
    page_text=$(ab get text body 2>/dev/null | head -c 2000)
    if echo "$page_text" | grep -qiE 'thank.you|received|submitted|under review|will review|pending approval'; then
      status="submitted"; reason="confirmed by text"
      shot="$shot_after"
    elif echo "$page_text" | grep -qiE 'upgrade.to|subscribe|paid plan|pro plan|premium|\$[0-9]+/mo'; then
      status="skipped"; reason="paid-only"
      shot="$shot_after"
    else
      status="unknown"; reason="no clear confirmation"
    fi
  fi

  log "  ✓ $status${reason:+ ($reason)} → $final_url"
  append_history "$(jq -nc --arg tu "$TOOL_URL" --arg tn "$TOOL_NAME" --arg dn "$dir_name" --arg du "$dir_url" --arg su "$submit_url" --arg st "$status" --arg rn "$reason" --arg sh "$shot" --arg fu "$final_url" --arg at "$now" \
    '{tool_url:$tu, tool_name:$tn, directory_name:$dn, directory_url:$du, submit_url:$su, status:$st, reason:$rn, screenshot:$sh, final_url:$fu, submitted_at:$at}')"
}

# Main loop
total=$(jq 'length' "$LIST")
log "Starting run: tool=$TOOL_NAME ($TOOL_URL), $total directories, start=$START, limit=$LIMIT"

idx=0
while IFS= read -r line; do
  idx=$((idx + 1))
  [ "$idx" -lt "$START" ] && continue
  [ "$LIMIT" -gt 0 ] && [ "$idx" -ge $((START + LIMIT)) ] && break

  name=$(echo "$line" | jq -r .name)
  url=$(echo "$line" | jq -r .url)

  log "[$idx/$total]"
  submit_one "$name" "$url"
  # Render report after each directory so user can peek mid-run
  python3 "$SKILL/scripts/render_report.py" "$HISTORY" -o "$WS/submission-report.html" >/dev/null 2>&1 || true
  sleep 2  # politeness between directories
done < <(jq -c '.[]' "$LIST")

# Optional: save the final auth state so next run can skip logins
if [ -n "$STATE_FILE" ]; then
  log "saving auth state to $STATE_FILE"
  ab state save "$STATE_FILE" >/dev/null || true
fi

agent-browser --session "$SESSION" close >/dev/null 2>&1 || true
log "DONE. Report: $WS/submission-report.html"
