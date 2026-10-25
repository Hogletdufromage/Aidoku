#!/bin/bash
#
# Xcode Cloud runs this right after cloning. It points the upstream project at
# my own Apple Developer account at build time, so no upstream file is ever
# changed in git and merges from Aidoku/Aidoku never conflict.
#
# Required environment variables (set in the Xcode Cloud workflow):
#   AIDOKU_BUNDLE_PREFIX  bundle ID of the app, e.g. com.arthur.aidoku
#   DEVELOPMENT_TEAM      Apple Developer Team ID (secret)

set -euo pipefail

: "${AIDOKU_BUNDLE_PREFIX:?is not set (expected e.g. com.arthur.aidoku)}"
: "${DEVELOPMENT_TEAM:?is not set (expected a 10-character Team ID)}"

fail() { echo "error: $*" >&2; exit 1; }

[[ "$AIDOKU_BUNDLE_PREFIX" =~ ^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$ ]] \
  || fail "AIDOKU_BUNDLE_PREFIX is not a valid bundle ID: $AIDOKU_BUNDLE_PREFIX"
[[ "$DEVELOPMENT_TEAM" =~ ^[A-Z0-9]{10}$ ]] \
  || fail "DEVELOPMENT_TEAM is not a 10-character Team ID"

ROOT="${CI_PRIMARY_REPOSITORY_PATH:-$(cd "$(dirname "$0")/.." && pwd)}"
XCCONFIG="$ROOT/Aidoku/Aidoku.xcconfig"
PBXPROJ="$ROOT/Aidoku.xcodeproj/project.pbxproj"
INFOPLIST="$ROOT/Aidoku/Info.plist"

for f in "$XCCONFIG" "$PBXPROJ" "$INFOPLIST"; do
  [[ -f "$f" ]] || fail "missing $f (did the upstream layout change?)"
done

# Bundle ID. The iCloud container and BGTask identifiers derive from it.
grep -qE '^PRODUCT_BUNDLE_IDENTIFIER[[:space:]]*=' "$XCCONFIG" \
  || fail "no PRODUCT_BUNDLE_IDENTIFIER line in $XCCONFIG"
sed -i '' -E "s/^PRODUCT_BUNDLE_IDENTIFIER[[:space:]]*=.*/PRODUCT_BUNDLE_IDENTIFIER   = $AIDOKU_BUNDLE_PREFIX/" "$XCCONFIG"

# Team.
grep -q 'DEVELOPMENT_TEAM = ' "$PBXPROJ" || fail "no DEVELOPMENT_TEAM in $PBXPROJ"
sed -i '' -E "s/DEVELOPMENT_TEAM = [A-Z0-9]+;/DEVELOPMENT_TEAM = $DEVELOPMENT_TEAM;/" "$PBXPROJ"
if grep 'DEVELOPMENT_TEAM = ' "$PBXPROJ" | grep -vq "DEVELOPMENT_TEAM = $DEVELOPMENT_TEAM;"; then
  fail "a DEVELOPMENT_TEAM entry in $PBXPROJ was not replaced"
fi

# With CANONICAL_BUILD the app compares itself to upstream's bundle ID and
# labels itself as sideloaded. This build has its own iCloud container.
sed -i '' -E 's/[[:space:]]*-DCANONICAL_BUILD//' "$XCCONFIG"

# Export compliance, so TestFlight does not ask about encryption on each build.
if ! /usr/libexec/PlistBuddy -c 'Print :ITSAppUsesNonExemptEncryption' "$INFOPLIST" >/dev/null 2>&1; then
  /usr/libexec/PlistBuddy -c 'Add :ITSAppUsesNonExemptEncryption bool false' "$INFOPLIST"
fi

# A dependency ships the SwiftLint package plugin, which Xcode Cloud cannot
# approve interactively. Only on Xcode Cloud: never touch a local Xcode setup.
if [[ "${CI:-}" == "TRUE" ]]; then
  defaults write com.apple.dt.Xcode IDESkipPackagePluginFingerprintValidation -bool YES
fi

echo "Aidoku retargeted:"
echo "  app bundle ID     $AIDOKU_BUNDLE_PREFIX"
echo "  iCloud container  iCloud.$AIDOKU_BUNDLE_PREFIX"
echo "Bundle IDs in project files:"
grep -hE '^[[:space:]]*PRODUCT_BUNDLE_IDENTIFIER' "$XCCONFIG" "$PBXPROJ" | sed -E 's/^[[:space:]]*/  /' | sort -u
