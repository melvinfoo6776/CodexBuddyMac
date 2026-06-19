#!/usr/bin/env bash
# Pre-push secret scan for CodexBuddyMac.
#
# Fails (exit 1) if likely secret VALUES or credential files are present in the
# working tree, so they are caught locally before a push. This complements
# GitHub's server-side push protection; run it before pushing.
#
#   scripts/check-secrets.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

# Real secret VALUES (token formats / private keys), not field names.
PATTERNS='sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]+'

# Files that must never be committed.
FORBIDDEN='(^|/)(\.env|auth\.json|\.credentials\.json|\.bridge_token|bridge\.pid|[^/]+\.p12|[^/]+\.pem|[^/]+\.mobileprovision|[^/]+\.keychain(-db)?)$'

fail=0

# Limit to the pushable tree: skip gitignored local-only trees so we only flag
# what would actually reach GitHub.
PRUNE='./.git/* ./.claude/* ./scripts/* ./App/* ./Widget/* ./Shared/* ./LaunchAgent/* ./SampleData/* ./Logs/* ./Dist/* ./build/* ./DerivedData/*'
find_args=(. -type f)
for p in $PRUNE; do find_args+=(-not -path "$p"); done

while IFS= read -r f; do
  [ -n "$f" ] || continue
  echo "FORBIDDEN FILE: $f"
  fail=1
done < <(find "${find_args[@]}" 2>/dev/null | grep -E "$FORBIDDEN" || true)

matches=$(grep -rInE "$PATTERNS" . \
  --exclude-dir=.git --exclude-dir=.claude --exclude-dir=scripts --exclude-dir=App --exclude-dir=Widget \
  --exclude-dir=Shared --exclude-dir=LaunchAgent --exclude-dir=SampleData \
  --exclude-dir=Logs --exclude-dir=Dist --exclude-dir=build --exclude-dir=DerivedData \
  --binary-files=without-match 2>/dev/null || true)
if [ -n "$matches" ]; then
  echo "POSSIBLE SECRET VALUES:"
  echo "$matches"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "❌ Secret scan FAILED — resolve the findings above before pushing."
  exit 1
fi
echo "✅ Secret scan clean — safe to push."
