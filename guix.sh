#!/bin/bash
set -e

if [ $# -lt 3 ]; then
    SCRIPT_NAME="$(basename "$0")"
    echo "Usage: $SCRIPT_NAME <project> <version> <remote>" >&2
    echo "Project must be one of: bitcoin, sv2-tp" >&2
    echo "Example: $SCRIPT_NAME bitcoin 28.0 guix" >&2
    exit 1
fi

PROJECT="$1"
VERSION="$2"
REMOTE="$3"

set -x

echo "Project: $PROJECT"
echo "Version: $VERSION"
echo "Remote: $REMOTE"

case "$PROJECT" in
  bitcoin)
    SIGS_REPO="guix.sigs"
    ;;
  sv2-tp)
    SIGS_REPO="sv2-tp-guix.sigs"
    ;;
  *)
    echo "Invalid project: $PROJECT"
    echo "Project must be one of: bitcoin, sv2-tp"
    exit 1
    ;;
esac

cd "$HOME/$SIGS_REPO"
git checkout main
git pull
mkdir -p "$VERSION"
scp -r "$REMOTE:$SIGS_REPO/$VERSION/Sjors" "$VERSION"
if [ -f "$VERSION/Sjors/all.SHA256SUMS" ]; then
  echo "Code signed"
  CODESIGNED=1
fi
if [ "$CODESIGNED" == "1" ]; then
  BRANCH="$VERSION-sjors-codesigned"
else
  BRANCH="$VERSION-sjors"
fi
git checkout -b $BRANCH
cd "$VERSION/Sjors"
if [ ! -f noncodesigned.SHA256SUMS.asc ]; then
  gpg --armor --detach-sign noncodesigned.SHA256SUMS
fi
if [ -f all.SHA256SUMS ]; then
  gpg --armor --detach-sign all.SHA256SUMS
fi
git add .
if [ "$CODESIGNED" != "1" ]; then
  git commit -a -m "Add sjors attestations for v$VERSION unsigned"
else
  git commit -a -m "Add sjors attestations for v$VERSION codesigned"
fi
if [ $SIGS_REPO == "guix.sigs" ]; then
  git push --set-upstream sjors $BRANCH
else
  git push --set-upstream origin $BRANCH
fi
