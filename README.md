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

## `guix-try`

Fetch a macOS guix build from a remote SSH host, extract it, ad hoc codesign it, and open it in Finder. Auto-detects arm64 vs x86_64. Run from a Bitcoin Core checkout.

```sh
cd ~/bitcoin
~/utils/guix-try            # Bitcoin-Qt.app (default)
~/utils/guix-try --bitcoind # CLI binaries
~/utils/guix-try --help     # all options and environment variables
```
