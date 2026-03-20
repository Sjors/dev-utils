#!/bin/zsh

set -euo pipefail

repo_dir="${BITCOIN_DIR:-$PWD}"
build_dir="${BUILD_DIR:-build_tidy}"

if jobs="$(sysctl -n hw.logicalcpu 2>/dev/null)"; then
  :
elif jobs="$(sysctl -n hw.ncpu 2>/dev/null)"; then
  :
else
  jobs="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
fi

if [[ ! -d "$repo_dir" ]]; then
  echo "Bitcoin Core checkout not found: $repo_dir" >&2
  exit 1
fi

if [[ -x /opt/homebrew/opt/llvm/bin/run-clang-tidy ]]; then
  run_clang_tidy=/opt/homebrew/opt/llvm/bin/run-clang-tidy
else
  run_clang_tidy="$(command -v run-clang-tidy 2>/dev/null || true)"
fi

if [[ -z "${run_clang_tidy:-}" ]]; then
  echo "run-clang-tidy not found. Install Homebrew llvm or add it to PATH." >&2
  exit 1
fi

files=()
if [[ "${1:-}" == "--all" ]]; then
  shift
elif (( $# > 0 )); then
  files=("$@")
else
  files=("ipc/libmultiprocess/src/mp/util.cpp")
fi

cd "$repo_dir"
cmake -B "$build_dir" -DCMAKE_EXPORT_COMPILE_COMMANDS=ON

sdkroot="$(xcrun --show-sdk-path)"

(
  cd src

  cmd=(
    "$run_clang_tidy"
    -p "../$build_dir"
    -quiet
    -config-file=.clang-tidy
    -extra-arg=-isysroot
    -extra-arg="$sdkroot"
    -j "$jobs"
  )

  if (( ${#files[@]} > 0 )); then
    cmd+=("${files[@]}")
  fi

  if ! "${cmd[@]}" | tee "../$build_dir/tidy-out.txt"; then
    echo
    echo "Failure generated from clang-tidy:"
    grep -C5 "error: " "../$build_dir/tidy-out.txt" || true
    exit 1
  fi
)
