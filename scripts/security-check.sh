#!/usr/bin/env bash
# CodexBuddyMac security check.
#
# On-demand audit of the things we care about for this app: no leaked secrets,
# the local bridge is loopback-only and authenticated, runtime files have safe
# permissions, and GitHub's protections are on. Safe to run any time — it only
# reads state and makes unauthenticated probes against the local bridge.
#
#   scripts/security-check.sh
#
# Exit code: 0 if no failures, 1 if any FAIL. WARN/SKIP do not fail the run.

cd "$(dirname "$0")/.." || exit 2

REPO_SLUG="melvinfoo6776/CodexBuddyMac"
BRIDGE_DIR="$HOME/Library/Application Support/CodexBuddy/Bridge"
LOG_DIR="$HOME/Library/Application Support/CodexBuddy/Logs"
PORT=8789

pass=0; failc=0; warnc=0
PASS() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; pass=$((pass+1)); }
FAIL() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; failc=$((failc+1)); }
WARN() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; warnc=$((warnc+1)); }
SKIP() { printf '  \033[90mSKIP\033[0m  %s\n' "$1"; }
INFO() { printf '  \033[36mINFO\033[0m  %s\n' "$1"; }
hdr()  { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

# --------------------------------------------------------------------------
hdr "1. Secrets & PII (working tree)"

if [ -x scripts/check-secrets.sh ]; then
  if scripts/check-secrets.sh >/tmp/cbm-secscan.out 2>&1; then
    PASS "secret scan clean (scripts/check-secrets.sh)"
  else
    FAIL "secret scan found issues:"; sed 's/^/        /' /tmp/cbm-secscan.out
  fi
else
  WARN "scripts/check-secrets.sh not found or not executable"
fi

# Home paths / email in *pushable* source only (LICENSE handle is intentional).
# Gitignored local-only trees (App/, LaunchAgent/, the obsolete root notes, etc.)
# are never pushed, so exclude them to mirror what actually lands on GitHub.
pii=$(grep -rInE "/Users/[a-z][a-z0-9_-]+/|[A-Za-z0-9._%+-]+@(gmail|icloud|outlook|hotmail)\." \
  --include='*.swift' --include='*.py' --include='*.json' --include='*.plist' \
  --include='*.pbxproj' --include='*.md' \
  --exclude-dir=.git --exclude-dir=.claude --exclude-dir=App --exclude-dir=Widget --exclude-dir=Shared \
  --exclude-dir=LaunchAgent --exclude-dir=SampleData --exclude-dir=Logs \
  --exclude-dir=Dist --exclude-dir=build --exclude-dir=DerivedData . 2>/dev/null \
  | grep -vE 'apple\.com/DTD|/LICENSE:|/PROJECT_STATUS\.md:|/STARTUP_OPTIONS\.md:|/XCODE_SETUP\.md:' || true)
if [ -z "$pii" ]; then
  PASS "no home paths or personal emails in tracked source"
else
  FAIL "possible PII in tracked source:"; echo "$pii" | sed 's/^/        /' | head -10
fi

# .gitignore covers the sensitive runtime/credential files.
miss=""
for pat in '.bridge_token' '.bridge.lock' 'bridge.pid' 'bridge_state.json' '*.pem' '.env'; do
  grep -qF "$pat" .gitignore 2>/dev/null || miss="$miss $pat"
done
[ -z "$miss" ] && PASS ".gitignore covers credential/runtime files" \
                || WARN ".gitignore missing:$miss"

# --------------------------------------------------------------------------
hdr "2. Local bridge (runtime)"

if curl -fsS --max-time 4 "http://127.0.0.1:$PORT/usage.json" >/dev/null 2>&1; then
  PASS "bridge reachable on 127.0.0.1:$PORT (GET /usage.json = 200)"

  # Loopback-only bind, not 0.0.0.0 / wildcard.
  binds=$(lsof -nP -iTCP:$PORT 2>/dev/null | awk '/LISTEN/{print $9}')
  if echo "$binds" | grep -qE '127\.0\.0\.1:'"$PORT" && ! echo "$binds" | grep -qE '(\*|0\.0\.0\.0):'"$PORT"; then
    PASS "listener bound to loopback only ($binds)"
  else
    FAIL "listener not loopback-only: $binds"
  fi

  # UDP discovery should be disabled (no 8788 listener).
  if lsof -nP -iUDP:8788 2>/dev/null | grep -q .; then
    WARN "UDP discovery responder is listening on 8788 (expected disabled)"
  else
    PASS "UDP discovery disabled (nothing on 8788)"
  fi

  # State-changing endpoint must reject an unauthenticated request.
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "http://127.0.0.1:$PORT/claude/refresh")
  [ "$code" = "403" ] && PASS "POST /claude/refresh without token = 403 (auth enforced)" \
                       || FAIL "POST /claude/refresh without token = $code (expected 403)"

  # Wrong token also rejected.
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST \
         -H "X-CodexBuddy-Token: definitely-wrong" "http://127.0.0.1:$PORT/claude/refresh")
  [ "$code" = "403" ] && PASS "POST /claude/refresh with wrong token = 403" \
                       || FAIL "POST /claude/refresh with wrong token = $code (expected 403)"

  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
         "http://127.0.0.1:$PORT/health")
  [ "$code" = "403" ] && PASS "GET /health without token = 403 (auth enforced)" \
                       || FAIL "GET /health without token = $code (expected 403)"
else
  SKIP "bridge not reachable on 127.0.0.1:$PORT — start the app to run runtime checks"
fi

# Token file permissions.
if [ -f "$BRIDGE_DIR/.bridge_token" ]; then
  perms=$(stat -f '%Lp' "$BRIDGE_DIR/.bridge_token" 2>/dev/null)
  [ "$perms" = "600" ] && PASS ".bridge_token permissions are 600" \
                       || FAIL ".bridge_token permissions are $perms (expected 600)"
else
  SKIP ".bridge_token not present (bridge not started yet)"
fi

# Logs must not contain token values.
if [ -d "$LOG_DIR" ]; then
  if grep -rEl 'sk-ant-[A-Za-z0-9]|eyJ[A-Za-z0-9_-]{15,}\.' "$LOG_DIR" >/dev/null 2>&1; then
    FAIL "token-like values found in logs under $LOG_DIR"
  else
    PASS "no token values in bridge logs"
  fi
else
  SKIP "no log directory yet"
fi

# --------------------------------------------------------------------------
hdr "3. Dependencies"

deps=$(grep -rhoE "^(import|from) [a-zA-Z0-9_]+" \
  CodexBuddyMac/CodexBuddyMac/Bridge/codex_usage_server.py 2>/dev/null \
  | awk '{print $2}' | sort -u | tr '\n' ' ')
thirdparty=$(echo "$deps" | tr ' ' '\n' | grep -vE '^(__future__|argparse|copy|fcntl|hashlib|json|os|secrets|socket|ssl|subprocess|sys|threading|time|datetime|http|pathlib|typing|urllib|certifi)?$' || true)
if [ -z "$thirdparty" ]; then
  PASS "bridge uses Python stdlib only (no third-party imports)"
else
  WARN "non-stdlib imports detected: $thirdparty"
fi
{ [ -f requirements.txt ] || [ -f Pipfile ]; } && WARN "a Python dependency manifest exists — review its pins" \
                                               || PASS "no pip dependency manifest to audit"

# --------------------------------------------------------------------------
hdr "4. GitHub protections"

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  sa=$(gh api "repos/$REPO_SLUG" --jq '.security_and_analysis.secret_scanning_push_protection.status' 2>/dev/null)
  [ "$sa" = "enabled" ] && PASS "secret-scanning push protection enabled" \
                        || FAIL "push protection is '${sa:-unknown}' (expected enabled)"

  alerts=$(gh api "repos/$REPO_SLUG/secret-scanning/alerts" --jq 'length' 2>/dev/null)
  if [ -z "$alerts" ]; then
    WARN "could not read secret-scanning alerts (permissions?)"
  elif [ "$alerts" = "0" ]; then
    PASS "0 open secret-scanning alerts"
  else
    FAIL "$alerts open secret-scanning alert(s) — review on GitHub"
  fi

  vis=$(gh api "repos/$REPO_SLUG" --jq '.visibility' 2>/dev/null)
  INFO "repository visibility: $vis"
else
  SKIP "gh not available/authenticated — skipping GitHub checks"
fi

# --------------------------------------------------------------------------
printf '\n\033[1m== Summary ==\033[0m\n'
printf '  %d passed, %d failed, %d warnings\n' "$pass" "$failc" "$warnc"
if [ "$failc" -ne 0 ]; then
  printf '\033[31mSecurity check FAILED.\033[0m\n'; exit 1
fi
printf '\033[32mSecurity check passed.\033[0m\n'; exit 0
