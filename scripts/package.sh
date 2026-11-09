#!/bin/sh
# Build release assets in $1 (default dist/): ledger-<version>.tar.gz, install.sh, checksums.txt.
set -eu
cd "$(dirname "$0")/.."
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' ledger/__init__.py)
[ -n "$version" ] || { echo "package: no version in ledger/__init__.py" >&2; exit 1; }
out=${1:-dist}
stage=$out/stage/ledger-$version
rm -rf "$out" && mkdir -p "$stage/bin" "$stage/ledger"
cp bin/ledger "$stage/bin/"
cp ledger/*.py "$stage/ledger/"
cp README.md "$stage/"
tar -C "$out/stage" -czf "$out/ledger-$version.tar.gz" "ledger-$version"
rm -rf "$out/stage"
cp install.sh "$out/install.sh"
cd "$out"
if command -v sha256sum >/dev/null 2>&1; then sum="sha256sum"; else sum="shasum -a 256"; fi
$sum "ledger-$version.tar.gz" install.sh > checksums.txt
echo "$out/ledger-$version.tar.gz"
