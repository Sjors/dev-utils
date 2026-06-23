# Utilities

## `tidy-btc-mac.zsh`

Run `clang-tidy` against a Bitcoin Core checkout on macOS with the SDK path
wired in so Homebrew LLVM can analyze source files cleanly.

Default behavior:

- uses the current working directory as the Bitcoin Core checkout
- configures a `build_tidy` directory with `CMAKE_EXPORT_COMPILE_COMMANDS=ON`
- detects the host CPU count with `sysctl` and passes it to `run-clang-tidy` via `-j`
- targets `ipc/libmultiprocess/src/mp/util.cpp`
- adds the macOS SDK path via `xcrun --show-sdk-path` so Homebrew LLVM can
  find system headers

Install prerequisites with vanilla Homebrew:

```sh
brew install llvm cmake jq
```

Notes:

- Homebrew's `llvm` package provides `clang-tidy` and `run-clang-tidy`
- Xcode Command Line Tools must be installed so `xcrun --show-sdk-path` works:

```sh
xcode-select --install
```

Usage:

```sh
cd ~/src/bitcoin
~/utils/tidy-btc-mac.zsh
```

Override defaults if needed:

```sh
BITCOIN_DIR=~/src/bitcoin ~/utils/tidy-btc-mac.zsh
BUILD_DIR=build-tidy-debug ~/utils/tidy-btc-mac.zsh
~/utils/tidy-btc-mac.zsh --all
~/utils/tidy-btc-mac.zsh ipc/libmultiprocess/src/mp/util.cpp
```

## `git-range-diff-remote`

Range-diff local commits against the remote version of a PR.

Default mode compares local HEAD against the pushed remote ref (`@{push}`),
useful for reviewing your own changes before pushing. PR discovery first uses
the current `gh` repository context and then falls back to the pushed remote's
`owner/repo`, so PRs opened against your own fork are handled too. The current
repository lookup matches the exact head branch, which also handles
upstream-owned branches reviewed from a fork checkout. Base branch refs are
chosen by the smallest local commit count above a matching branch name, with a
warning if the PR repository's base ref appears stale.
`--since-ack` mode finds your last ACK/utACK comment on the PR and range-diffs
from that commit, useful when re-reviewing a PR after a force-push. The
`--since HASH` mode does the same from a caller-specified commit and, when
the old and new tips share a non-mainline commit, anchors the current side at
that shared commit. Otherwise, when possible, it anchors the current side at
the nearest mainline base instead of estimating it from the old commit count.
`--top` narrows the comparison to commits above an inferred stacked base
branch. Handles detached HEAD automatically.

Install by symlinking the script and man page:

```sh
mkdir -p ~/bin          # ensure this is on $PATH
mkdir -p ~/share/man/man1
ln -s ~/utils/git-range-diff-remote                  ~/bin/git-range-diff-remote
ln -s ~/utils/share/man/man1/git-range-diff-remote.1 \
  ~/share/man/man1/git-range-diff-remote.1
```

```sh
git range-diff-remote                   # local vs pushed
git range-diff-remote --since-ack       # local vs last ACK'd commit
git range-diff-remote --top             # only commits above the stacked base
git range-diff-remote --help            # full usage (man page)
```

## `guix-try`

Fetch a macOS guix build from a remote SSH host, extract it, ad hoc codesign it
if needed, and open it in Finder. Auto-detects arm64 vs x86_64. Run from a
`bitcoin` or `sv2-tp` checkout, or select the project explicitly.

```sh
cd ~/bitcoin
~/utils/guix-try            # Bitcoin-Qt.app (default)
~/utils/guix-try --bitcoind # CLI binaries

cd ~/dev/sv2-tp
~/utils/guix-try           # sv2-tp CLI binaries (default in sv2-tp checkout)
~/utils/guix-try --signed 1.1.0
~/utils/guix-try --install --signed 1.1.0

cd ~/utils
~/utils/guix-try --sv2-tp --signed 1.1.0

~/utils/guix-try --help     # all options and environment variables
```

## `verify_bitcoin_commits.sh`

Verify each commit in a Bitcoin Core range by checking it out, building, and
running selected unit and functional tests. By default it writes the full
terminal transcript to `tmp/verify-bitcoin-commits.log` in the Bitcoin Core
checkout, while still printing output to the terminal.

```sh
cd ~/src/bitcoin
~/utils/verify_bitcoin_commits.sh -r 5 -u descriptor_tests
~/utils/verify_bitcoin_commits.sh -r a1b2c3..HEAD \
  -u "miner_tests,txvalidation_tests" \
  -f "interface_ipc.py,interface_ipc_mining.py"
```

Useful options:

- pass `--log-file <path>` to choose a different log file
- pass `--no-log` to disable writing the log
- pass `--tmp-root <dir>` to move temp output and the default log location

## `bitcoin-test-temp-root.sh`

Print a temp root for Bitcoin Core test runs. On macOS it prefers
`/Volumes/RAMDisk`, creating the standard 11 GiB RAM disk when enough memory is
available. If the RAM disk is unavailable or memory is tight, it falls back to
`${TMPDIR:-/tmp}/bitcoin-tests`.

```sh
test_tmp_root=$(~/utils/bitcoin-test-temp-root.sh)
build/test/functional/test_runner.py --tmpdirprefix="$test_tmp_root"

~/utils/bitcoin-test-temp-root.sh --preserve
```

By default the selected temp root is cleared before use. Pass `--preserve` or
`--no-clear` to reuse existing temp contents.

## Tests

```sh
brew install bats-core
bats tests/
```
