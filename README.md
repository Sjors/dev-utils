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

## `git-fetch-sign`

Fetch a topic branch from an explicitly selected Git remote, sign its commits
with your local Git signing key, and replace the same local branch. Run from
any worktree; the utility finds and updates the branch's own worktree. It preserves
commit contents, metadata, dates, and merge topology, verifies every signature,
and keeps recovery refs for the fetched and previous local tips. After a
successful local sync, it pushes the signed branch back to that remote using a
force-with-lease tied to the exact fetched tip. `--remote NAME` is required;
the utility does not assume a source machine or remote name.
Use `--no-push` to keep the signed result local.

For SSH and local transports, the push uses Git's `updateInstead` mode for
that invocation only. This updates a checked-out remote branch and its
worktree together, including linked worktrees, without detaching the worktree
or changing remote configuration. Dirty remote worktrees refuse the push,
preserving their edits. A changed remote tip also refuses the push. In either
case, the signed local branch and recovery refs remain available.

Local staged, unstaged, and untracked changes are automatically stashed just
before replacing the branch and restored afterward, including their staging
state. Ignored files are excluded from the stash; existing stashes are preserved.
If stash restoration conflicts, the utility reverts the sync and restores the
original changes on the old branch tip, then exits with an error. If recovery
also fails, it keeps the stash and prints a recovery ref. Unresolved conflicts
and ongoing Git operations are refused. `--dry-run` leaves local changes in
place without stashing them.

```sh
cd /path/to/repository
git fetch-sign --remote workstation
git fetch-sign --remote workstation --dry-run feature/example
git fetch-sign --remote workstation feature/example
git fetch-sign --remote workstation --pr 123 --repo owner/repo
git fetch-sign --remote workstation \
  --pr https://github.com/owner/repo/pull/123 \
  --worktree ../repository-pr/123-topic
git fetch-sign --remote workstation --base origin/parent-branch stacked-topic
```

With no branch argument or `--pr`, use the current worktree's branch.
Detached HEAD requires an explicit branch name or `--pr NUMBER/URL`.
`--pr` uses `gh` to query GitHub's API for the head branch name, then fetches
it from the selected remote.
PR numbers use the current `gh` repository context; `--repo owner/repo`
selects a repository explicitly.

The branch must have a local worktree. If it has none, supply `--worktree PATH`
to create one after signing succeeds. The destination must not exist.
`--dry-run` also requires this option for a missing worktree, but creates
nothing. If the branch already has a worktree, an explicit path must match it.

Each run fetches `master` from `origin` first, falling back to `main` only
if the remote has no `master`. All commits reachable from that fetched tip
keep their original IDs and signatures, including upstream commits merged into
the topic branch. This works even with a stale `origin/HEAD` or a missing local
upstream tracking ref. An upstream fetch failure stops the sync.

`--upstream REMOTE/BRANCH` selects a different upstream. `--base REF` additionally
excludes a stacked parent branch's history; it cannot disable upstream protection.
`--remote` selects the source remote. A branch containing only upstream
commits is synced without recreating them. Signing topic commits changes their
IDs. The automatic push goes back to the selected source remote.

From this repository, install the Git subcommand and its manual:

```sh
mkdir -p ~/bin ~/share/man/man1
ln -s "$PWD/git-fetch-sign" ~/bin/git-fetch-sign
ln -s "$PWD/share/man/man1/git-fetch-sign.1" ~/share/man/man1/git-fetch-sign.1
git fetch-sign --help
```

## `guix.sh`

Fetch Guix attestation checksums over SSH, sign them locally with GnuPG, and
commit them in a signatures repository. Supports Bitcoin Core
([guix.sigs](https://github.com/bitcoin-core/guix.sigs)) and sv2-tp
([sv2-tp-guix.sigs](https://github.com/stratum-mining/sv2-tp-guix.sigs)).
Requires Bash, Git, OpenSSH `scp`, GnuPG, and
a configured secret signing key. This signs downloaded checksums; it does not
build the release, recompute checksums, or verify binaries. Review the build
and checksums before running it.

Set `SIGNER` to your attestation directory name (case-sensitive). There is no
default identity or push destination:

```sh
SIGNER=alice ~/utils/guix.sh bitcoin 31.0 build-host
SIGNER=alice ~/utils/guix.sh sv2-tp 1.1.0 build-host --push my-fork
~/utils/guix.sh --help
```

The SSH host is an alias or `[user@]hostname`; use an SSH config alias for
custom ports or IPv6 addresses. The Git remote passed to `--push` must already
exist in the local signatures repository. Without `--push`, the commit stays
local for review.

Environment overrides:

| Variable | Purpose | Default |
| --- | --- | --- |
| `SIGNER` | Attestation directory and branch/commit identity | Required |
| `SIGS_DIR` | Local signatures repository root | `$HOME/guix.sigs` or `$HOME/sv2-tp-guix.sigs` |
| `REMOTE_SIGS_DIR` | Absolute or home-relative remote repository path | `guix.sigs` or `sv2-tp-guix.sigs` |
| `BASE_BRANCH` | Local base branch with an upstream configured | `main` |
| `GPG_KEY` | GPG signing key fingerprint | GnuPG's default key |

For example, with custom checkout locations and an explicit signing key:

```sh
SIGNER=alice SIGS_DIR="$HOME/src/guix.sigs" \
  REMOTE_SIGS_DIR=/srv/build/guix.sigs GPG_KEY=YOUR_KEY_FINGERPRINT \
  ~/utils/guix.sh bitcoin 31.0 build-host --push my-fork
```

Replace `YOUR_KEY_FINGERPRINT` with your key fingerprint.
Signer names use letters, digits, underscores and hyphens, starting with a
letter or digit. Versions are exact directory names, starting with a letter
or digit and using letters, digits, dots, underscores or hyphens; they must
also produce a valid Git branch name. Remote paths cannot contain spaces or
shell metacharacters. Local paths may contain spaces.

The local repository must be clean, including untracked files, with no merge,
rebase or cherry-pick in progress. The script downloads into a temporary
directory, creates fresh local signatures, checks out `BASE_BRANCH`, and pulls
its upstream with `--ff-only`. It leaves you on a new `<version>-<SIGNER>`
branch, adding `-codesigned` when `all.SHA256SUMS` exists. Existing branches
are refused. Only `noncodesigned.SHA256SUMS`, optional `all.SHA256SUMS`, and
their new `.asc` signatures are copied and committed; other downloaded files
and remote signatures are excluded. `noncodesigned.SHA256SUMS` is required
even for codesigned builds. Temporary downloads are removed on exit. If a
later Git operation or push fails, inspect the local branch and working tree
before retrying; the script does not roll back Git operations.

Published attestations use the chosen signer name and GPG key. Commits also
use your configured Git name/email and any configured commit-signing key.

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
python3 -m unittest discover -s tests -p 'test_*.py'
```
