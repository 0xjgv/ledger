#!/bin/sh
# Bump the version (BUMP=patch|minor|major, default patch), commit, tag, and push main with the tag.
# The tag triggers .github/workflows/release.yml, which tests, packages, and publishes.
set -eu
cd "$(dirname "$0")/.."
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "release: run on main" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "release: commit or stash changes first" >&2; exit 1; }
current=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' ledger/__init__.py)
if git rev-parse -q --verify "refs/tags/v$current" >/dev/null && [ "$(git rev-list -n1 "v$current")" = "$(git rev-parse HEAD)" ]; then
    git push origin main "v$current"
    exit 0
fi
next=$(echo "$current" | awk -F. -v bump="${BUMP:-patch}" '{
    if (bump == "major") print $1 + 1 ".0.0"; else if (bump == "minor") print $1 "." $2 + 1 ".0"; else print $1 "." $2 "." $3 + 1 }')
if git rev-parse -q --verify "refs/tags/v$current" >/dev/null; then version=$next; else version=$current; fi
sed -i.bak "s/^__version__ = \".*\"$/__version__ = \"$version\"/" ledger/__init__.py && rm ledger/__init__.py.bak
python3.13 -m unittest discover -s tests
git add ledger/__init__.py
git diff --cached --quiet || git commit -qm "chore(release): v$version"
git tag "v$version"
git push --atomic origin main "v$version"
echo "Pushed v$version; the Release workflow publishes it."
