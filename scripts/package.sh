#!/bin/sh
# Build release assets in $1 (default dist/): ledger-<version>-py3-none-any.whl, install.sh, checksums.txt.
set -eu
cd "$(dirname "$0")/.."
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' ledger/__init__.py)
[ -n "$version" ] || { echo "package: no version in ledger/__init__.py" >&2; exit 1; }
out=${1:-dist}
wheel=ledger-$version-py3-none-any.whl
rm -rf "$out" build ledger.egg-info && mkdir -p "$out"
uv build --quiet --wheel --out-dir "$out"
rm -rf build ledger.egg-info "$out/.gitignore"
[ -f "$out/$wheel" ] || { echo "package: uv build did not write $out/$wheel" >&2; exit 1; }
cp install.sh "$out/install.sh"
cd "$out"
if command -v sha256sum >/dev/null 2>&1; then sum="sha256sum"; else sum="shasum -a 256"; fi
$sum "$wheel" install.sh > checksums.txt
echo "$out/$wheel"
