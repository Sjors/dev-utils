#!/usr/bin/env bats
# Tests for git-range-diff-remote.
#
# Install bats: brew install bats-core
# Run:          bats tests/git-range-diff-remote.bats

SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/git-range-diff-remote"
FIXTURES="$(cd "$(dirname "$BATS_TEST_FILENAME")" && pwd)/fixtures"
MOCK_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")" && pwd)"

setup() {
    cd "$BATS_TEST_TMPDIR"

    export GIT_AUTHOR_NAME="Test"
    export GIT_AUTHOR_EMAIL="test@example.com"
    export GIT_COMMITTER_NAME="Test"
    export GIT_COMMITTER_EMAIL="test@example.com"
    # Suppress global config interference
    export GIT_CONFIG_GLOBAL=/dev/null

    git init -q
    echo "base" > base.txt && git add base.txt
    git commit -q -m "base"
    export BASE_SHA
    BASE_SHA="$(git rev-parse HEAD)"

    # Inject mock gh ahead of the real one
    export PATH="$MOCK_DIR:$PATH"
    export MOCK_LOGIN="testuser"
    export MOCK_PR_NUMBER="42"
}

# Helper: create a PR commit on a detached HEAD with a single remote-tracking ref.
# Sets PR_SHA and leaves HEAD detached at that commit.
make_pr_head() {
    echo "pr work" > pr.txt && git add pr.txt
    git commit -q -m "PR commit"
    export PR_SHA
    PR_SHA="$(git rev-parse HEAD)"
    git remote add w0xlt "https://github.com/w0xlt/bitcoin.git"
    git update-ref "refs/remotes/w0xlt/ipc-submit-block" HEAD
    git checkout -q --detach HEAD
}

# ---------------------------------------------------------------------------
# Detached HEAD behaviour
# ---------------------------------------------------------------------------

@test "default mode: detached HEAD with one remote ref suggests checkout" {
    make_pr_head

    run "$SCRIPT" 2>&1
    [ "$status" -eq 1 ]
    [[ "$output" == *"git checkout ipc-submit-block"* ]]
}

@test "default mode: detached HEAD with multiple remote refs lists all options" {
    make_pr_head
    git remote add origin "https://github.com/bitcoin/bitcoin.git"
    git update-ref "refs/remotes/origin/ipc-submit-block" HEAD

    run "$SCRIPT" 2>&1
    [ "$status" -eq 1 ]
    [[ "$output" == *"git checkout"*"ipc-submit-block"* ]]
    # Both remotes should be suggested
    [[ "$(printf '%s\n' "$output" | grep -c 'git checkout')" -ge 2 ]]
}

# ---------------------------------------------------------------------------
# --since-ack mode
# ---------------------------------------------------------------------------

@test "--since-ack: detached HEAD with one remote ref does not die with checkout hint" {
    make_pr_head
    export MOCK_HEAD_SHA="$PR_SHA"
    export MOCK_PR_JSON="$FIXTURES/pr_no_ack.json"

    run "$SCRIPT" --since-ack 2>&1
    [ "$status" -eq 1 ]
    [[ "$output" != *"Detached HEAD"* ]]
}

@test "--since-ack: no ACK in PR exits with clear message" {
    make_pr_head
    export MOCK_HEAD_SHA="$PR_SHA"
    export MOCK_PR_JSON="$FIXTURES/pr_no_ack.json"

    run "$SCRIPT" --since-ack 2>&1
    [ "$status" -eq 1 ]
    [[ "$output" == *"No ACK comment found"* ]]
}

@test "--since-ack: ACK comment with SHA runs range-diff" {
    # Create old version of the PR commit (the one that was ACK'd)
    echo "old pr work" > pr.txt && git add pr.txt
    git commit -q -m "old PR version"
    export ACK_SHA
    ACK_SHA="$(git rev-parse HEAD)"

    # Reset and create the new (current) version, branched from the same base
    git reset -q --hard "$BASE_SHA"
    echo "new pr work" > pr.txt && git add pr.txt
    git commit -q -m "new PR version"
    export PR_SHA
    PR_SHA="$(git rev-parse HEAD)"

    git remote add w0xlt "https://github.com/w0xlt/bitcoin.git"
    git update-ref "refs/remotes/w0xlt/ipc-submit-block" HEAD
    git checkout -q --detach HEAD

    export MOCK_HEAD_SHA="$PR_SHA"
    export MOCK_ACK_SHA="$ACK_SHA"
    export MOCK_PR_JSON="$FIXTURES/pr_with_ack.json"

    run "$SCRIPT" --since-ack 2>&1
    [ "$status" -eq 0 ]
    [[ "$output" == *"Using PREV=$ACK_SHA"* ]]
}
