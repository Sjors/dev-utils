# Utilities

## `tidy-btc-mac.zsh`

Run `clang-tidy` against a Bitcoin Core checkout on macOS with the SDK path wired in so Homebrew LLVM can analyze source files cleanly.

Default behavior:

- uses the current working directory as the Bitcoin Core checkout
- configures a `build_tidy` directory with `CMAKE_EXPORT_COMPILE_COMMANDS=ON`
- detects the host CPU count with `sysctl` and passes it to `run-clang-tidy` via `-j`
- targets `ipc/libmultiprocess/src/mp/util.cpp`
- adds the macOS SDK path via `xcrun --show-sdk-path` so Homebrew LLVM can find system headers

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

Default mode compares local HEAD against the pushed remote ref (`@{push}`), useful for reviewing your own changes before pushing. `--since-ack` mode finds your last ACK/utACK comment on the PR and range-diffs from that commit, useful when re-reviewing a PR after a force-push. `--top` narrows the comparison to commits above an inferred stacked base branch. Handles detached HEAD automatically.

Install by symlinking the script and man page:

```sh
mkdir -p ~/bin          # ensure this is on $PATH
mkdir -p ~/share/man/man1
ln -s ~/utils/git-range-diff-remote                  ~/bin/git-range-diff-remote
ln -s ~/utils/share/man/man1/git-range-diff-remote.1  ~/share/man/man1/git-range-diff-remote.1
```

```sh
git range-diff-remote                   # local vs pushed
git range-diff-remote --since-ack       # local vs last ACK'd commit
git range-diff-remote --top             # only commits above the stacked base
git range-diff-remote --help            # full usage (man page)
```

## `guix-try`

Fetch a macOS guix build from a remote SSH host, extract it, ad hoc codesign it if needed, and open it in Finder. Auto-detects arm64 vs x86_64. Run from a `bitcoin` or `sv2-tp` checkout, or select the project explicitly.

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

## Tests

```sh
brew install bats-core
bats tests/
```
