#!/bin/sh
# Install or update ledger from a checksummed GitHub release.
#   curl -fsSL https://github.com/0xjgv/ledger/releases/latest/download/install.sh | sh
# LEDGER_VERSION        release version, e.g. v0.1.0 (default: latest)
# LEDGER_DOWNLOAD_BASE  release base URL, or a directory holding the assets (testing)
# LEDGER_HOME           where release wheels are kept (default: $HOME/.local/share/ledger)
# LEDGER_BIN_DIR        where uv puts the ledger command (default: `uv tool dir --bin`, usually $HOME/.local/bin)
# uv installs the wheel as a tool and downloads Python 3.11+ when none is present.
set -eu

REPO=0xjgv/ledger
die() { echo "ledger install: $*" >&2; exit 1; }

command -v uv >/dev/null 2>&1 || die "uv is required: https://docs.astral.sh/uv/"
if [ -n "${LEDGER_BIN_DIR:-}" ]; then
    UV_TOOL_BIN_DIR=$LEDGER_BIN_DIR
    export UV_TOOL_BIN_DIR
fi

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
    else die "sha256sum or shasum is required"; fi
}

version=${LEDGER_VERSION:-}
version=${version#v}
if [ -n "$version" ]; then
    printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || die "LEDGER_VERSION must look like v0.1.0"
fi
base=${LEDGER_DOWNLOAD_BASE:-https://github.com/$REPO/releases}
base=${base%/}
case $base in
/*) release=$base ;;
*) if [ -n "$version" ]; then release=$base/download/v$version; else release=$base/latest/download; fi ;;
esac

fetch() {
    case $release in
    /*) cp "$release/$1" "$2" 2>/dev/null && return 0 ;;
    *) curl -fsSL "$release/$1" -o "$2" 2>/dev/null && return 0 ;;
    esac
    die "download failed: $release/$1"
}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' 0
trap 'exit 1' HUP INT TERM

fetch checksums.txt "$tmp/checksums.txt"
wheel=$(awk -v v="$version" '{sub(/^\*/, "", $2)} $2 ~ /^ledger-[0-9]+\.[0-9]+\.[0-9]+-py3-none-any\.whl$/ && (v == "" || $2 == "ledger-" v "-py3-none-any.whl") {f = $2; n++} END {if (n == 1) print f}' "$tmp/checksums.txt")
[ -n "$wheel" ] || die "expected one ledger wheel in $release/checksums.txt"
expected=$(awk -v name="$wheel" '{sub(/^\*/, "", $2)} $2 == name {print $1}' "$tmp/checksums.txt")
fetch "$wheel" "$tmp/$wheel"
[ "$(sha256 "$tmp/$wheel")" = "$expected" ] || die "checksum mismatch for $wheel"
wheel_version=${wheel#ledger-}
wheel_version=${wheel_version%-py3-none-any.whl}

# uv records the wheel's path, so keep it at a stable location.
home=${LEDGER_HOME:-$HOME/.local/share/ledger}
mkdir -p "$home/releases"
cp "$tmp/$wheel" "$home/releases/$wheel"

# Releases before the uv installer unpacked into $home/ledger-<version> and linked the command there.
bin_dir=$(uv tool dir --bin)
if [ -L "$bin_dir/ledger" ]; then
    case $(readlink "$bin_dir/ledger") in "$home"/ledger-*/bin/ledger) rm -f "$bin_dir/ledger" ;; esac
fi
for old in "$home"/ledger-[0-9]*; do
    if [ -d "$old" ]; then rm -rf "$old"; fi
done

uv tool install --quiet --force --python '>=3.11' "$home/releases/$wheel" || die "uv tool install failed for $wheel"
installed=$("$bin_dir/ledger" version) || die "installed ledger does not run"
[ "$installed" = "$wheel_version" ] || die "installed ledger reported $installed"
echo "Installed $bin_dir/ledger: $installed"
case :$PATH: in
*":$bin_dir:"*) ;;
*) echo "Add $bin_dir to PATH: export PATH=\"$bin_dir:\$PATH\"" ;;
esac
