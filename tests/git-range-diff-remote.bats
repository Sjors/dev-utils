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
    unset MOCK_PR_NUMBERS MOCK_PR_JSON MOCK_BASE_PR MOCK_BASE_HEAD MOCK_OPEN_BRANCHES
    unset MOCK_PR_REPO
    unset MOCK_HEAD_SHA MOCK_ACK_SHA MOCK_VIEW_PR_NUMBER MOCK_VIEW_HEAD_REF_NAME MOCK_VIEW_HEAD_REPO_OWNER
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

make_stacked_branch() {
    git remote add origin "https://github.com/testuser/bitcoin.git"

    echo "base pr 1" > stacked.txt && git add stacked.txt
    git commit -q -m "base PR 1"
    echo "base pr 2" >> stacked.txt && git add stacked.txt
    git commit -q -m "base PR 2"
    export STACK_BASE_SHA
    STACK_BASE_SHA="$(git rev-parse HEAD)"

    git branch -q stacked-base

    echo "top pr 1" >> stacked.txt && git add stacked.txt
    git commit -q -m "top PR 1"
    echo "top pr 2" >> stacked.txt && git add stacked.txt
    git commit -q -m "top PR 2"
    export LOCAL_HEAD_SHA
    LOCAL_HEAD_SHA="$(git rev-parse HEAD)"

    git branch -q feature-top
    git update-ref refs/remotes/origin/feature-top HEAD~1
    git branch --set-upstream-to=origin/feature-top feature-top >/dev/null
    git checkout -q feature-top
}

make_rebased_merge_pr() {
    git remote add w0xlt "https://github.com/w0xlt/bitcoin.git"

    git checkout -q -b old-base
    echo "base 1" >> base.txt && git add base.txt
    git commit -q -m "base 1"
    echo "base 2" >> base.txt && git add base.txt
    git commit -q -m "base 2"
    echo "base 3" >> base.txt && git add base.txt
    git commit -q -m "base 3"
    export OLD_BASE_SHA
    OLD_BASE_SHA="$(git rev-parse HEAD)"

    git checkout -q -b old-payload "$BASE_SHA"
    echo "old payload" > payload.txt && git add payload.txt
    git commit -q -m "old payload"
    export OLD_PAYLOAD_SHA
    OLD_PAYLOAD_SHA="$(git rev-parse HEAD)"

    git checkout -q -b pr-branch "$OLD_BASE_SHA"
    git merge --no-ff -q -m "merge old payload" "$OLD_PAYLOAD_SHA"
    export ACK_SHA
    ACK_SHA="$(git rev-parse HEAD)"

    git checkout -q -B new-payload "$BASE_SHA"
    echo "new payload" > payload.txt && git add payload.txt
    git commit -q -m "new payload"
    export NEW_PAYLOAD_SHA
    NEW_PAYLOAD_SHA="$(git rev-parse HEAD)"

    git checkout -q -B pr-branch "$BASE_SHA"
    git merge --no-ff -q -m "merge new payload" "$NEW_PAYLOAD_SHA"
    export PR_SHA
    PR_SHA="$(git rev-parse HEAD)"

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

@test "--since-ack: detached HEAD prefers canonical branch over .N-rebase ref" {
    make_pr_head
    git update-ref "refs/remotes/w0xlt/ipc-submit-block.5-rebase" HEAD
    export MOCK_HEAD_SHA="$PR_SHA"
    export MOCK_PR_JSON="$FIXTURES/pr_no_ack.json"

    run "$SCRIPT" --since-ack 2>&1
    [ "$status" -eq 1 ]
    [[ "$output" != *"Detached HEAD"* ]]
    [[ "$output" == *"No ACK comment found"* ]]
}

@test "--since-ack: no ACK in PR exits with clear message" {
    make_pr_head
    export MOCK_HEAD_SHA="$PR_SHA"
    export MOCK_PR_JSON="$FIXTURES/pr_no_ack.json"

    run "$SCRIPT" --since-ack 2>&1
    [ "$status" -eq 1 ]
    [[ "$output" == *"No ACK comment found"* ]]
}

@test "--since-ack: ambiguous PRs require --pr" {
    make_pr_head
    export MOCK_PR_NUMBERS="34804 34952"

    run "$SCRIPT" --since-ack 2>&1
    [ "$status" -eq 1 ]
    [[ "$output" == *"Ambiguous pull request"* ]]
    [[ "$output" == *"--pr 34804"* ]]
    [[ "$output" == *"--pr 34952"* ]]
}

@test "--since-ack: --pr selects one ambiguous PR" {
    make_pr_head
    export MOCK_PR_NUMBERS="34804 34952"
    export MOCK_VIEW_PR_NUMBER="34952"
    export MOCK_VIEW_HEAD_REF_NAME="ipc-submit-block"
    export MOCK_VIEW_HEAD_REPO_OWNER="w0xlt"
    export MOCK_HEAD_SHA="$PR_SHA"
    export MOCK_PR_JSON="$FIXTURES/pr_no_ack.json"

    run "$SCRIPT" --since-ack --pr 34952 2>&1
    [ "$status" -eq 1 ]
    [[ "$output" == *"No ACK comment found"* ]]
    [[ "$output" != *"Ambiguous pull request"* ]]
}

@test "--since: runs range-diff from specified commit" {
    echo "old pr work" > pr.txt && git add pr.txt
    git commit -q -m "old PR version"
    export SINCE_SHA
    SINCE_SHA="$(git rev-parse HEAD)"

    git reset -q --hard "$BASE_SHA"
    echo "new pr work" > pr.txt && git add pr.txt
    git commit -q -m "new PR version"

    git remote add w0xlt "https://github.com/w0xlt/bitcoin.git"
    git update-ref "refs/remotes/w0xlt/ipc-submit-block" HEAD
    git checkout -q --detach HEAD

    run "$SCRIPT" --since "$SINCE_SHA" 2>&1
    [ "$status" -eq 0 ]
    [[ "$output" == *"Using PREV=$SINCE_SHA N=1"* ]]
}

@test "--since: dies with clear message when commit is ancestor of HEAD" {
    make_pr_head

    run "$SCRIPT" --since "$BASE_SHA" 2>&1
    [ "$status" -eq 1 ]
    [[ "$output" == *"ancestor of HEAD"* ]]
}

@test "--since: shows fetch attempt and hint when commit is unavailable" {
    make_pr_head

    run "$SCRIPT" --since "deadbeef1234" 2>&1
    [ "$status" -eq 1 ]
    [[ "$output" == *"Fetching deadbeef1234 from"* ]]
    [[ "$output" == *"not available locally"* ]]
    [[ "$output" == *"tried fetching from"* ]]
}

@test "--since and --since-ack are mutually exclusive" {
    make_pr_head

    run "$SCRIPT" --since-ack --since deadbeef 2>&1
    [ "$status" -eq 1 ]
    [[ "$output" == *"mutually exclusive"* ]]
}

@test "unknown top-level git-range-diff option is rejected with guidance" {
    make_pr_head

    run "$SCRIPT" --no-color 2>&1
    [ "$status" -eq 1 ]
    [[ "$output" == *"Unknown option: --no-color"* ]]
    [[ "$output" == *"Pass git-range-diff options after --"* ]]
}

@test "default mode: falls back to push remote repo for fork-owned PR" {
    git remote add origin "https://github.com/wizardsardine/async-hwi.git"
    git remote add sjors "git@github.com:Sjors/async-hwi.git"
    git update-ref refs/remotes/sjors/2026/04/hwi-rs "$BASE_SHA"

    git checkout -q -b 2026/04/musig
    echo "old pr work" > pr.txt && git add pr.txt
    git commit -q -m "old PR version"
    git update-ref refs/remotes/sjors/2026/04/musig HEAD
    git branch --set-upstream-to=sjors/2026/04/musig 2026/04/musig >/dev/null

    git reset -q --hard "$BASE_SHA"
    echo "new pr work" > pr.txt && git add pr.txt
    git commit -q -m "new PR version"
    export PR_SHA
    PR_SHA="$(git rev-parse HEAD)"

    export MOCK_PR_REPO="Sjors/async-hwi"
    export MOCK_OPEN_BRANCHES="2026/04/musig"
    export MOCK_PR_JSON="$FIXTURES/pr_one_commit_default.json"
    export MOCK_HEAD_SHA="$PR_SHA"
    export MOCK_VIEW_HEAD_REF_NAME="2026/04/musig"
    export MOCK_VIEW_HEAD_REPO_OWNER="Sjors"

    run "$SCRIPT" 2>&1
    [ "$status" -eq 0 ]
    [[ "$output" == *"old PR version"* ]]
    [[ "$output" == *"new PR version"* ]]

    run "$SCRIPT" --pr 42 2>&1
    [ "$status" -eq 0 ]
    [[ "$output" != *"missing --repo"* ]]
    [[ "$output" == *"old PR version"* ]]
    [[ "$output" == *"new PR version"* ]]
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

@test "--since-ack: merge-tip PR rebased backwards compares merge payload only" {
    make_rebased_merge_pr

    export MOCK_HEAD_SHA="$PR_SHA"
    export MOCK_ACK_SHA="$ACK_SHA"
    export MOCK_PR_JSON="$FIXTURES/pr_with_ack.json"

    run "$SCRIPT" --since-ack 2>&1
    [ "$status" -eq 0 ]
    [[ "$output" == *"Warning: PR appears to have been rebased onto an older commit"* ]]
    [[ "$output" == *"Using PREV=$ACK_SHA MERGE_PAYLOAD=1"* ]]
    [[ "$output" == *"old payload"* ]]
    [[ "$output" == *"new payload"* ]]
    [[ "$output" != *"base 1"* ]]
    [[ "$output" != *"base 2"* ]]
    [[ "$output" != *"base 3"* ]]
}

@test "--top: default mode only considers commits above the stacked base" {
    make_stacked_branch
    export MOCK_PR_JSON="$FIXTURES/pr_default.json"
    export MOCK_OPEN_BRANCHES="feature-top stacked-base"

    run "$SCRIPT" --top 2>&1
    [ "$status" -eq 0 ]
    [[ "$output" == *"Using PUSH="*"TOP_BASE=$STACK_BASE_SHA"* ]]
    [[ "$output" == *"top PR 1"* ]]
    [[ "$output" == *"top PR 2"* ]]
    [[ "$output" != *"base PR 1"* ]]
    [[ "$output" != *"base PR 2"* ]]
}

@test "--top: prefers explicit base PR from the PR description" {
    make_stacked_branch
    git branch -D stacked-base >/dev/null
    git checkout -q --detach "$STACK_BASE_SHA"
    echo "old top version 1" >> stacked.txt && git add stacked.txt
    git commit -q -m "old top PR 1"
    echo "old top version 2" >> stacked.txt && git add stacked.txt
    git commit -q -m "old top PR 2"
    git update-ref refs/remotes/origin/feature-top HEAD
    git checkout -q feature-top
    export MOCK_PR_JSON="$FIXTURES/pr_with_based_on.json"
    export MOCK_BASE_PR="32876"
    export MOCK_BASE_HEAD="$STACK_BASE_SHA"
    export MOCK_BASE_COUNT="2"

    run "$SCRIPT" --top 2>&1
    [ "$status" -eq 0 ]
    [[ "$output" == *"Using PUSH="*"TOP_BASE=$STACK_BASE_SHA"* ]]
    [[ "$output" == *"top PR 1"* ]]
    [[ "$output" == *"top PR 2"* ]]
    [[ "$output" != *"base PR 1"* ]]
    [[ "$output" != *"base PR 2"* ]]
}

@test "--top: since mode only considers commits above the stacked base" {
    make_stacked_branch
    export MOCK_OPEN_BRANCHES="feature-top stacked-base"

    git checkout -q --detach "$STACK_BASE_SHA"
    echo "old top version 1" >> stacked.txt && git add stacked.txt
    git commit -q -m "old top PR 1"
    echo "old top version 2" >> stacked.txt && git add stacked.txt
    git commit -q -m "old top PR 2"
    export OLD_TOP_SHA
    OLD_TOP_SHA="$(git rev-parse HEAD)"
    git checkout -q feature-top
    export MOCK_PR_JSON="$FIXTURES/pr_with_based_on.json"
    export MOCK_BASE_PR="32876"
    export MOCK_BASE_HEAD="$STACK_BASE_SHA"
    export MOCK_BASE_COUNT="2"

    run "$SCRIPT" --since "$OLD_TOP_SHA" --top 2>&1
    [ "$status" -eq 0 ]
    [[ "$output" == *"Using PREV=$OLD_TOP_SHA TOP_BASE=$STACK_BASE_SHA"* ]]
    [[ "$output" == *"top PR 2"* ]]
    [[ "$output" != *"base PR 1"* ]]
    [[ "$output" != *"base PR 2"* ]]
}

@test "--top: heuristic ignores local ancestor branches without an open PR" {
    make_stacked_branch
    export MOCK_PR_JSON="$FIXTURES/pr_default.json"
    export MOCK_OPEN_BRANCHES="feature-top"

    run "$SCRIPT" --top 2>&1
    [ "$status" -eq 1 ]
    [[ "$output" == *"Could not infer a stacked base for --top"* ]]
}
