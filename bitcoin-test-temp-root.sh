#!/usr/bin/env bash
set -euo pipefail

ramdisk=${BITCOIN_TEST_RAMDISK:-/Volumes/RAMDisk}
ramdisk_name=${BITCOIN_TEST_RAMDISK_NAME:-RAMDisk}
ramdisk_size_sectors=${BITCOIN_TEST_RAMDISK_SIZE_SECTORS:-23068672}
ramdisk_min_free_bytes=${BITCOIN_TEST_RAMDISK_MIN_FREE_BYTES:-19327352832}
clear=1

usage() {
    cat <<'EOF'
Usage: bitcoin-test-temp-root.sh [--preserve]

Print a Bitcoin Core test temp root.

By default, prefer /Volumes/RAMDisk, clear it before use, and create the
standard 11 GiB RAM disk when absent and enough RAM is free. Fall back to the
private temp directory when memory is tight or RAM disk tools are unavailable.

Options:
  --preserve, --no-clear   Reuse existing temp contents instead of clearing.
  -h, --help               Show this help.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --preserve|--no-clear)
            clear=0
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'unknown option: %s\n' "$arg" >&2
            usage >&2
            exit 2
            ;;
    esac
done

ramdisk_mounted() {
    mount | awk -v path="$ramdisk" '$3 == path { found = 1 } END { exit !found }'
}

free_memory_bytes() {
    if ! command -v vm_stat >/dev/null || ! command -v pagesize >/dev/null; then
        printf '0\n'
        return
    fi

    vm_stat | awk -v pagesize="$(pagesize)" '
        /Pages free/ {
            gsub(/\./, "", $3)
            free = $3
        }
        /Pages speculative/ {
            gsub(/\./, "", $3)
            speculative = $3
        }
        END {
            printf "%.0f\n", (free + speculative) * pagesize
        }
    '
}

clear_path() {
    local path=$1

    if [ "$clear" -eq 1 ]; then
        rm -rf "$path"
    fi
}

clear_ramdisk() {
    if [ "$clear" -eq 1 ]; then
        find "$ramdisk" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    fi
}

create_ramdisk() {
    local device

    device=$(hdiutil attach -nomount "ram://${ramdisk_size_sectors}")
    if ! diskutil erasevolume HFS+ "$ramdisk_name" "$device" >/dev/null; then
        hdiutil detach "$device" >/dev/null || true
        return 1
    fi
}

can_create_ramdisk() {
    command -v hdiutil >/dev/null && command -v diskutil >/dev/null
}

if ramdisk_mounted; then
    clear_ramdisk
    test_tmp_root="$ramdisk/bitcoin-tests"
elif can_create_ramdisk &&
    [ "$(free_memory_bytes)" -ge "$ramdisk_min_free_bytes" ]; then
    create_ramdisk
    test_tmp_root="$ramdisk/bitcoin-tests"
else
    test_tmp_root="${TMPDIR:-/tmp}/bitcoin-tests"
    clear_path "$test_tmp_root"
fi

mkdir -p "$test_tmp_root" "$test_tmp_root/unit"
printf '%s\n' "$test_tmp_root"
