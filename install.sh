#!/bin/sh
# Install or update ledger from a checksummed GitHub release.
#   curl -fsSL https://github.com/0xjgv/ledger/releases/latest/download/install.sh | sh
# LEDGER_VERSION        release version, e.g. v0.1.0 (default: latest)
# LEDGER_DOWNLOAD_BASE  release base URL, or a directory holding the assets (testing)
# LEDGER_HOME           where versions are unpacked (default: $HOME/.local/share/ledger)
# LEDGER_BIN_DIR        where the ledger command is linked (default: $HOME/.local/bin)
set -eu

REPO=0xjgv/ledger
die() { echo "ledger install: $*" >&2; exit 1; }

python=
for p in python3.13 python3.12 python3.11 python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        python=$p
        break
    fi
done
[ -n "$python" ] || die "Python 3.11 or newer is required"

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
archive=$(awk -v v="$version" '{sub(/^\*/, "", $2)} $2 ~ /^ledger-[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz$/ && (v == "" || $2 == "ledger-" v ".tar.gz") {f = $2; n++} END {if (n == 1) print f}' "$tmp/checksums.txt")
[ -n "$archive" ] || die "expected one ledger archive in $release/checksums.txt"
expected=$(awk -v name="$archive" '{sub(/^\*/, "", $2)} $2 == name {print $1}' "$tmp/checksums.txt")
fetch "$archive" "$tmp/$archive"
[ "$(sha256 "$tmp/$archive")" = "$expected" ] || die "checksum mismatch for $archive"

name=${archive%.tar.gz}
home=${LEDGER_HOME:-$HOME/.local/share/ledger}
bin_dir=${LEDGER_BIN_DIR:-$HOME/.local/bin}
mkdir -p "$home" "$bin_dir"
rm -rf "$home/.unpack"
mkdir "$home/.unpack"
tar -xzf "$tmp/$archive" -C "$home/.unpack"
rm -rf "${home:?}/$name"
mv "$home/.unpack/$name" "$home/$name"
rm -rf "$home/.unpack"
ln -sf "$home/$name/bin/ledger" "$bin_dir/ledger"

installed=$("$bin_dir/ledger" version) || die "installed ledger does not run"
[ "$installed" = "${name#ledger-}" ] || die "installed ledger reported $installed"
echo "Installed $bin_dir/ledger: $installed"
case :$PATH: in
*":$bin_dir:"*) ;;
*) echo "Add $bin_dir to PATH: export PATH=\"$bin_dir:\$PATH\"" ;;
esac
