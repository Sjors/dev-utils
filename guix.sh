#!/bin/bash
set -euo pipefail

usage() {
    cat <<HELP
Usage: $(basename "$0") <project> <version> <ssh-host> [--push <git-remote>]

Fetch and sign Guix checksums, then commit them on a new local branch.
Project must be bitcoin or sv2-tp. Push only when --push is supplied.

Environment:
  SIGNER           Required attestation directory and branch/commit identity
  SIGS_DIR         Local repository (default: \$HOME/<project signatures repo>)
  REMOTE_SIGS_DIR  Remote repository path (default: <project signatures repo>)
  BASE_BRANCH      Local base branch with upstream configured (default: main)
  GPG_KEY          Signing key fingerprint (default: GnuPG's default key)

Example: SIGNER=alice $(basename "$0") bitcoin 31.0 build-host --push my-fork

This signs downloaded checksums; it does not build or verify the binaries.
HELP
}

die() {
    echo "Error: $*" >&2
    exit 1
}

if [[ $# -eq 1 && ( "$1" == --help || "$1" == -h ) ]]; then
    usage
    exit 0
fi
if [[ $# -ne 3 && $# -ne 5 ]]; then
    usage >&2
    exit 1
fi

PROJECT="$1"
VERSION="$2"
REMOTE="$3"
PUSH_REMOTE=""
if [[ $# -eq 5 ]]; then
    [[ "$4" == --push && -n "$5" && "$5" != -* ]] || die "Expected --push <git-remote>"
    PUSH_REMOTE="$5"
fi

case "$PROJECT" in
    bitcoin) SIGS_REPO="guix.sigs" ;;
    sv2-tp) SIGS_REPO="sv2-tp-guix.sigs" ;;
    *) die "Project must be bitcoin or sv2-tp" ;;
esac

SIGNER="${SIGNER:-}"
SIGS_DIR="${SIGS_DIR:-$HOME/$SIGS_REPO}"
REMOTE_SIGS_DIR="${REMOTE_SIGS_DIR:-$SIGS_REPO}"
BASE_BRANCH="${BASE_BRANCH:-main}"

# These values become path components, Git refs, or scp's remote specification.
[[ "$SIGNER" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || die "Set SIGNER to a name containing letters, digits, underscores or hyphens"
[[ "$VERSION" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || die "Invalid version directory"
[[ "$REMOTE" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.@-]*$ ]] || die "Use an SSH alias or [user@]hostname"
# Keep paths safe with both SFTP scp and legacy scp's remote shell.
[[ "$REMOTE_SIGS_DIR" =~ ^[a-zA-Z0-9_./-]+$ && "$REMOTE_SIGS_DIR" != -* ]] || die "Remote path must use only letters, digits, underscores, dots, slashes or hyphens"

for command in git scp gpg mktemp; do
    command -v "$command" >/dev/null || die "Required command not found: $command"
done
git check-ref-format --branch "$BASE_BRANCH" >/dev/null || die "Invalid base branch"
git check-ref-format --branch "$VERSION-$SIGNER" >/dev/null || die "Invalid version or signer for a branch name"

cd "$SIGS_DIR"
[[ "$(git rev-parse --show-toplevel)" == "$(pwd -P)" ]] || die "SIGS_DIR must be the repository root"
[[ -z "$(git status --porcelain --untracked-files=all)" ]] || die "Signatures repository must be clean (including untracked files)"
for operation in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
    [[ ! -e "$(git rev-parse --git-path "$operation")" ]] || die "Finish the ongoing Git operation first"
done
if [[ -n "$PUSH_REMOTE" ]]; then
    git remote get-url --push "$PUSH_REMOTE" >/dev/null || die "Unknown push remote: $PUSH_REMOTE"
fi

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/guix-attest.XXXXXXXX")
trap 'rm -rf -- "$WORK_DIR"' EXIT
scp -r "$REMOTE:$REMOTE_SIGS_DIR/$VERSION/$SIGNER" "$WORK_DIR/download"

SUMS=(noncodesigned.SHA256SUMS)
BUILD_TYPE="unsigned"
BRANCH="$VERSION-$SIGNER"
if [[ -e "$WORK_DIR/download/all.SHA256SUMS" ]]; then
    SUMS+=(all.SHA256SUMS)
    BUILD_TYPE="codesigned"
    BRANCH+="-codesigned"
fi
git show-ref --verify --quiet "refs/heads/$BRANCH" && die "Branch already exists: $BRANCH"

GPG_ARGS=(--armor --detach-sign)
if [[ -n "${GPG_KEY:-}" ]]; then
    GPG_ARGS+=(--local-user "$GPG_KEY")
fi
for checksum in "${SUMS[@]}"; do
    [[ -s "$WORK_DIR/download/$checksum" && ! -L "$WORK_DIR/download/$checksum" ]] || die "Missing, empty or symlinked checksum file: $checksum"
    # Always sign locally; never reuse signatures copied from the build host.
    gpg "${GPG_ARGS[@]}" --output "$WORK_DIR/$checksum.asc" -- "$WORK_DIR/download/$checksum"
done

git checkout "$BASE_BRANCH"
git pull --ff-only --no-rebase
git checkout -b "$BRANCH"
DESTINATION="$VERSION/$SIGNER"
mkdir -p "$DESTINATION"
for checksum in "${SUMS[@]}"; do
    cp "$WORK_DIR/download/$checksum" "$DESTINATION/$checksum"
    cp "$WORK_DIR/$checksum.asc" "$DESTINATION/$checksum.asc"
    git add -- "$DESTINATION/$checksum" "$DESTINATION/$checksum.asc"
done
git commit -m "Add $SIGNER attestations for v$VERSION $BUILD_TYPE"

if [[ -n "$PUSH_REMOTE" ]]; then
    git push --set-upstream "$PUSH_REMOTE" "$BRANCH"
else
    echo "Committed on $BRANCH. Review, then push with: git push --set-upstream <remote> $BRANCH"
fi
