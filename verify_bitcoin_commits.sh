#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  verify_bitcoin_commits.sh -r <range_or_count> [options]

Options:
  -r, --range <value>       Commit range (e.g. A..B) or count N (last N commits)
  -u, --unit <tests>        Unit tests for test_bitcoin (comma-separated or repeatable)
  -f, --functional <tests>  Functional tests (comma-separated or repeatable)
  -b, --build-cmd <cmd>     Build command (default: "cmake --build build -j 16")
      --configure-cmd <cmd> Configure command when build/ is missing
                             (default: auto-detect dev-mode-no-gui, then dev-mode)
  --no-build            Skip build step
      --tmp-root <dir>      Temp root (default: <repo>/tmp)
      --worktree <dir>      Verification worktree (default: <tmp-root>/verify-bitcoin-commits-worktree)
      --fresh-worktree      Remove and recreate the verification worktree before running
      --remove-worktree     Remove the verification worktree on exit
      --cachedir <dir>      Functional test cache dir (default: <tmp-root>/cache-functional)
      --log-file <path>     Log file (default: <tmp-root>/verify-bitcoin-commits.log)
      --no-log              Do not write a log file
      --remote <host>       Copy this script to <host> and run it there over ssh
      --remote-repo <dir>   Remote git worktree/repository to run from
      --remote-script <path>
                             Remote script path (default: <remote-repo>/tmp/verify_bitcoin_commits.sh)
      --no-remote-copy      Do not copy this script to the remote host before running
      --tmux-session <name> Remote tmux session name (default: verify-bitcoin-commits)
  -h, --help                Show this help

Examples:
  verify_bitcoin_commits.sh -r 5 -u descriptor_tests
  verify_bitcoin_commits.sh -r a1b2c3..HEAD -u "miner_tests,txvalidation_tests" -f "interface_ipc.py,interface_ipc_mining.py"
  verify_bitcoin_commits.sh --remote copilot --remote-repo /home/copilot/bitcoin-master --tmux-session 33966 -r master..HEAD -u miner_tests
EOF
}

append_csv() {
    local value="$1"
    local -n out_ref="$2"
    local item
    IFS=',' read -r -a items <<< "$value"
    for item in "${items[@]}"; do
        item="${item#"${item%%[![:space:]]*}"}"
        item="${item%"${item##*[![:space:]]}"}"
        [[ -z "$item" ]] && continue
        out_ref+=("$item")
    done
}

sanitize_name() {
    echo "$1" | tr -c '[:alnum:]. _-' '_' | tr ' ' '_'
}

shell_quote() {
    printf '%q' "$1"
}

join_quoted() {
    local item
    local first=1
    for item in "$@"; do
        if [[ "$first" -eq 0 ]]; then
            printf ' '
        fi
        shell_quote "$item"
        first=0
    done
}

RANGE_INPUT=""
BUILD_CMD="cmake --build build -j 16"
CONFIGURE_CMD=""
DO_BUILD=1
declare -a UNIT_TESTS=()
declare -a FUNCTIONAL_TESTS=()
TMP_ROOT=""
CACHEDIR=""
LOG_FILE=""
DO_LOG=1
VERIFY_WORKTREE=""
FRESH_WORKTREE=0
REMOVE_WORKTREE_ON_EXIT=0
REMOTE_HOST=""
REMOTE_REPO=""
REMOTE_SCRIPT=""
REMOTE_COPY_SCRIPT=1
TMUX_SESSION="verify-bitcoin-commits"
declare -a FORWARD_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -r|--range)
            RANGE_INPUT="${2:-}"
            FORWARD_ARGS+=("$1" "${2:-}")
            shift 2
            ;;
        -u|--unit)
            append_csv "${2:-}" UNIT_TESTS
            FORWARD_ARGS+=("$1" "${2:-}")
            shift 2
            ;;
        -f|--functional)
            append_csv "${2:-}" FUNCTIONAL_TESTS
            FORWARD_ARGS+=("$1" "${2:-}")
            shift 2
            ;;
        -b|--build-cmd)
            BUILD_CMD="${2:-}"
            FORWARD_ARGS+=("$1" "${2:-}")
            shift 2
            ;;
        --configure-cmd)
            CONFIGURE_CMD="${2:-}"
            FORWARD_ARGS+=("$1" "${2:-}")
            shift 2
            ;;
        --no-build)
            DO_BUILD=0
            FORWARD_ARGS+=("$1")
            shift
            ;;
        --tmp-root)
            TMP_ROOT="${2:-}"
            FORWARD_ARGS+=("$1" "${2:-}")
            shift 2
            ;;
        --cachedir)
            CACHEDIR="${2:-}"
            FORWARD_ARGS+=("$1" "${2:-}")
            shift 2
            ;;
        --worktree)
            VERIFY_WORKTREE="${2:-}"
            FORWARD_ARGS+=("$1" "${2:-}")
            shift 2
            ;;
        --fresh-worktree)
            FRESH_WORKTREE=1
            FORWARD_ARGS+=("$1")
            shift
            ;;
        --remove-worktree)
            REMOVE_WORKTREE_ON_EXIT=1
            FORWARD_ARGS+=("$1")
            shift
            ;;
        --log-file)
            LOG_FILE="${2:-}"
            FORWARD_ARGS+=("$1" "${2:-}")
            shift 2
            ;;
        --no-log)
            DO_LOG=0
            FORWARD_ARGS+=("$1")
            shift
            ;;
        --remote)
            REMOTE_HOST="${2:-}"
            shift 2
            ;;
        --remote-repo)
            REMOTE_REPO="${2:-}"
            shift 2
            ;;
        --remote-script)
            REMOTE_SCRIPT="${2:-}"
            shift 2
            ;;
        --no-remote-copy)
            REMOTE_COPY_SCRIPT=0
            shift
            ;;
        --tmux-session)
            TMUX_SESSION="${2:-}"
            shift 2
            ;;
        --tmux-session=*)
            TMUX_SESSION="${1#*=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ -n "$REMOTE_HOST" ]]; then
    if [[ -z "$REMOTE_REPO" ]]; then
        echo "Error: --remote-repo is required with --remote" >&2
        exit 1
    fi
    if [[ -z "$REMOTE_SCRIPT" ]]; then
        REMOTE_SCRIPT="$REMOTE_REPO/tmp/verify_bitcoin_commits.sh"
    fi
    if [[ -z "$TMUX_SESSION" ]]; then
        echo "Error: --tmux-session must not be empty" >&2
        exit 1
    fi
    remote_script_dir="$(dirname "$REMOTE_SCRIPT")"
    if [[ "$REMOTE_COPY_SCRIPT" -eq 1 ]]; then
        ssh "$REMOTE_HOST" "mkdir -p $(shell_quote "$remote_script_dir")"
        scp "$0" "$REMOTE_HOST:$REMOTE_SCRIPT" >/dev/null
        ssh "$REMOTE_HOST" "chmod +x $(shell_quote "$REMOTE_SCRIPT")"
    fi
    remote_cmd="cd $(shell_quote "$REMOTE_REPO") && $(join_quoted "$REMOTE_SCRIPT" "${FORWARD_ARGS[@]}")"
    if ssh "$REMOTE_HOST" "tmux has-session -t $(shell_quote "$TMUX_SESSION")" >/dev/null 2>&1; then
        echo "Error: remote tmux session already exists: $TMUX_SESSION" >&2
        echo "Attach with: ssh $REMOTE_HOST tmux attach -t $(shell_quote "$TMUX_SESSION")" >&2
        exit 1
    fi
    ssh "$REMOTE_HOST" "tmux new-session -d -s $(shell_quote "$TMUX_SESSION") -- bash -lc $(shell_quote "$remote_cmd")"
    echo "Started remote verification on $REMOTE_HOST in tmux session '$TMUX_SESSION'."
    echo "Attach: ssh $REMOTE_HOST tmux attach -t $(shell_quote "$TMUX_SESSION")"
    exit 0
fi

if [[ -z "$RANGE_INPUT" ]]; then
    echo "Error: --range is required" >&2
    usage >&2
    exit 1
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Error: must run inside a git repository" >&2
    exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"
REALPWD="$(pwd -P)"

if [[ -z "$TMP_ROOT" ]]; then
    TMP_ROOT="$REALPWD/tmp"
fi
if [[ -z "$CACHEDIR" ]]; then
    CACHEDIR="$TMP_ROOT/cache-functional"
fi
if [[ -z "$VERIFY_WORKTREE" ]]; then
    VERIFY_WORKTREE="$TMP_ROOT/verify-bitcoin-commits-worktree"
fi
if [[ -z "$LOG_FILE" ]]; then
    LOG_FILE="$TMP_ROOT/verify-bitcoin-commits.log"
fi

mkdir -p "$TMP_ROOT" "$CACHEDIR"
if [[ "$DO_LOG" -eq 1 ]]; then
    mkdir -p "$(dirname "$LOG_FILE")"
    exec > >(tee "$LOG_FILE") 2>&1
fi

if [[ "$RANGE_INPUT" =~ ^[0-9]+$ ]]; then
    if [[ "$RANGE_INPUT" -eq 0 ]]; then
        echo "Error: count must be >= 1" >&2
        exit 1
    fi
    RANGE_EXPR="HEAD~${RANGE_INPUT}..HEAD"
elif [[ "$RANGE_INPUT" == *".."* ]]; then
    RANGE_EXPR="$RANGE_INPUT"
else
    RANGE_EXPR="${RANGE_INPUT}^..${RANGE_INPUT}"
fi

mapfile -t COMMITS < <(git rev-list --reverse "$RANGE_EXPR")
if [[ "${#COMMITS[@]}" -eq 0 ]]; then
    echo "Error: no commits found for range '$RANGE_EXPR'" >&2
    exit 1
fi

START_HEAD="$(git rev-parse HEAD)"
START_BRANCH="$(git symbolic-ref --short -q HEAD || true)"
SOURCE_REPO_ROOT="$REALPWD"

restore_ref() {
    cd "$SOURCE_REPO_ROOT" >/dev/null 2>&1 || true
    if [[ "$REMOVE_WORKTREE_ON_EXIT" -eq 1 ]] && git worktree list --porcelain | grep -Fxq "worktree $VERIFY_WORKTREE"; then
        git worktree remove --force "$VERIFY_WORKTREE" >/dev/null 2>&1 || true
    fi
}
trap restore_ref EXIT

if [[ "$FRESH_WORKTREE" -eq 1 ]] && git worktree list --porcelain | grep -Fxq "worktree $VERIFY_WORKTREE"; then
    git worktree remove --force "$VERIFY_WORKTREE" >/dev/null
fi

if git worktree list --porcelain | grep -Fxq "worktree $VERIFY_WORKTREE"; then
    if [[ -n "$(git -C "$VERIFY_WORKTREE" status --porcelain --untracked-files=no)" ]]; then
        echo "Error: verification worktree has local changes: $VERIFY_WORKTREE" >&2
        echo "Use --fresh-worktree to remove and recreate it." >&2
        exit 1
    fi
    git -C "$VERIFY_WORKTREE" checkout --detach "$START_HEAD" >/dev/null
else
    if [[ -e "$VERIFY_WORKTREE" ]]; then
        echo "Error: verification worktree path exists but is not registered: $VERIFY_WORKTREE" >&2
        exit 1
    fi
    git worktree add --detach "$VERIFY_WORKTREE" "$START_HEAD" >/dev/null
fi
REPO_ROOT="$VERIFY_WORKTREE"
cd "$REPO_ROOT"
REALPWD="$(pwd -P)"

echo "Source:    $SOURCE_REPO_ROOT"
echo "Worktree:  $REALPWD"
echo "Range:     $RANGE_EXPR"
echo "Commits:   ${#COMMITS[@]}"
echo "Build:     $([[ "$DO_BUILD" -eq 1 ]] && echo "$BUILD_CMD" || echo "skipped")"
echo "Configure: $([[ "$DO_BUILD" -eq 1 ]] && echo "${CONFIGURE_CMD:-auto}" || echo "skipped")"
echo "Unit:      ${UNIT_TESTS[*]:-none}"
echo "Functional:${FUNCTIONAL_TESTS[*]:-none}"
echo "Log:       $([[ "$DO_LOG" -eq 1 ]] && echo "$LOG_FILE" || echo "disabled")"
echo "Cleanup:   $([[ "$REMOVE_WORKTREE_ON_EXIT" -eq 1 ]] && echo "remove worktree on exit" || echo "keep worktree")"
echo

for commit in "${COMMITS[@]}"; do
    subject="$(git show -s --format=%s "$commit")"
    echo "== Verifying $commit $subject"
    git checkout --detach "$commit" >/dev/null

    if [[ "$DO_BUILD" -eq 1 ]]; then
        if [[ ! -f build/CMakeCache.txt ]]; then
            if [[ -z "$CONFIGURE_CMD" ]]; then
                if cmake --list-presets 2>/dev/null | grep -Fq '"dev-mode-no-gui"'; then
                    CONFIGURE_CMD="cmake --preset dev-mode-no-gui"
                elif cmake --list-presets 2>/dev/null | grep -Fq '"dev-mode"'; then
                    CONFIGURE_CMD="cmake --preset dev-mode"
                else
                    CONFIGURE_CMD="cmake -B build"
                fi
            fi
            eval "$CONFIGURE_CMD"
        fi
        eval "$BUILD_CMD"
    fi

    for unit_test in "${UNIT_TESTS[@]}"; do
        echo "-- unit: $unit_test"
        build/bin/test_bitcoin --run_test="$unit_test"
    done

    for functional_test in "${FUNCTIONAL_TESTS[@]}"; do
        script_path="$functional_test"
        if [[ "$script_path" != */* ]]; then
            script_path="build/test/functional/$script_path"
        fi
        if [[ ! -f "$script_path" ]]; then
            if [[ "$script_path" != *.py && -f "${script_path}.py" ]]; then
                script_path="${script_path}.py"
            else
                echo "Error: functional test script not found: $functional_test" >&2
                exit 1
            fi
        fi

        test_name="$(basename "$script_path" .py)"
        safe_test_name="$(sanitize_name "$test_name")"
        tmpdir="$TMP_ROOT/func-${safe_test_name}-${commit:0:12}"

        echo "-- functional: $script_path"
        rm -rf "$tmpdir"
        env PWD="$REALPWD" TMPDIR="$TMP_ROOT" \
            "$script_path" \
            --tmpdir="$tmpdir" \
            --cachedir="$CACHEDIR"
    done
done

echo
echo "All requested commits verified successfully."
