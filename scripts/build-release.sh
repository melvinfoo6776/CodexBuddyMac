#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

developer_dir="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
derived_data="${DERIVED_DATA_DIR:-$PWD/build/ReleaseDerivedData}"
release_dir="$PWD/Dist/Release"
app_name="CodexBuddyMac.app"
archive_name="CodexBuddyMac-1.1.1.zip"

DEVELOPER_DIR="$developer_dir" xcodebuild \
  -project CodexBuddyMac/CodexBuddyMac.xcodeproj \
  -scheme CodexBuddyMac \
  -configuration Release \
  -derivedDataPath "$derived_data" \
  CODE_SIGNING_ALLOWED=NO \
  build \
  -quiet

mkdir -p "$release_dir"
rm -rf "$release_dir/$app_name" "$release_dir/$archive_name"
ditto "$derived_data/Build/Products/Release/$app_name" "$release_dir/$app_name"
xattr -cr "$release_dir/$app_name"

# Ad-hoc signing makes the local test bundle internally verifiable. A public
# release still requires a Developer ID Application certificate and notarization.
codesign --force --sign - --timestamp=none "$release_dir/$app_name"
codesign --verify --deep --strict --verbose=2 "$release_dir/$app_name"

ditto -c -k --norsrc --noextattr --keepParent \
  "$release_dir/$app_name" "$release_dir/$archive_name"
(cd "$release_dir" && shasum -a 256 "$archive_name" > SHA256SUMS.txt)

verify_dir="$(mktemp -d /tmp/codexbuddy-release-verify.XXXXXX)"
trap 'rm -rf "$verify_dir"' EXIT
ditto -x -k "$release_dir/$archive_name" "$verify_dir"
codesign --verify --deep --strict --verbose=2 "$verify_dir/$app_name"
rm -rf "$verify_dir"
trap - EXIT

printf 'Release candidate: %s\n' "$release_dir/$app_name"
printf 'Archive: %s\n' "$release_dir/$archive_name"
cat "$release_dir/SHA256SUMS.txt"
