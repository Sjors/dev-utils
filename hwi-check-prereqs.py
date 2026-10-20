#!/usr/bin/env python3
"""Report Ubuntu packages recommended for native HWI simulator builds.

Read-only: never invokes sudo or installs anything. Firmware/toolchain pins and
runtime shared libraries still need checking against the selected HWI checkout.
"""

import argparse
import fnmatch
import os
import shlex
import shutil
import subprocess
import sys


DEVICES = (
    'bitbox01', 'bitbox02', 'coldcard', 'coldcard-edge', 'keepkey',
    'trezor-1', 'trezor-t', 'ledger', 'ledger-legacy', 'jade',
)
BASE = (
    'build-essential', 'cmake', 'ninja-build', 'pkg-config', 'git', 'curl',
    'unzip', 'ca-certificates', 'python3-dev', 'python3-venv',
    'libffi-dev', 'libssl-dev', 'libusb-1.0-0-dev',
)
FAMILY_PACKAGES = {
    'bitbox01': (),
    'bitbox02': (),  # Its compiler/SDK comes from the firmware's pinned image.
    'coldcard': (
        'gcc-13', 'autoconf', 'automake', 'libtool', 'libsdl2-dev',
        'libpcsclite-dev', 'swig',
    ),
    'keepkey': ('gcc-13', 'g++-13', 'protobuf-compiler', 'libprotobuf-dev'),
    'trezor-1': ('gcc-12', 'libsdl2-dev', 'libsdl2-image-dev',
                 'protobuf-compiler', 'libprotobuf-dev'),
    'trezor-t': ('libsdl2-dev', 'libsdl2-image-dev', 'protobuf-compiler',
                 'libprotobuf-dev', ('libclang-dev', 'libclang-[0-9]*-dev')),
    'ledger': (
        'gcc-arm-linux-gnueabihf', 'libc6-dev-armhf-cross', 'libsdl2-dev',
        'libsdl2-image-dev', 'libvncserver-dev', 'libltdl-dev',
        ('qemu-user-static', 'qemu-user'),
    ),
    'jade': (
        'autoconf', 'automake', 'libtool', 'libgcrypt20-dev', 'libsdl2-dev',
        'libslirp-dev', 'libjpeg-dev', 'zlib1g-dev', 'libncurses-dev',
        'flex', 'bison', 'gperf', 'ccache', 'libudev-dev', 'rsync',
        'bzip2', 'xz-utils',
    ),
}


def requirements(devices):
    result = [(package,) for package in BASE]
    for device in devices:
        family = {'coldcard-edge': 'coldcard', 'ledger-legacy': 'ledger'}.get(device, device)
        for package in FAMILY_PACKAGES[family]:
            alternatives = (package,) if isinstance(package, str) else package
            if alternatives not in result:
                result.append(alternatives)
    return result


def parse_installed(output):
    # Strip multiarch suffixes; residual config and unpacked packages don't count.
    return {
        name.split(':')[0]
        for line in output.splitlines()
        for name, status in [line.split('\t', 1)]
        if status == 'installed'
    }


def missing_requirements(required, installed):
    return [
        alternatives for alternatives in required
        if not any(fnmatch.fnmatchcase(name, pattern)
                   for name in installed for pattern in alternatives)
    ]


def apt_candidate(package):
    result = subprocess.run(
        ['apt-cache', 'policy', package], capture_output=True, text=True,
        env={**os.environ, 'LC_ALL': 'C'}, check=True,
    )
    return any(line.strip().startswith('Candidate:') and
               line.split(':', 1)[1].strip() != '(none)'
               for line in result.stdout.splitlines())


def install_packages(missing, candidate):
    available, unavailable = [], []
    for alternatives in missing:
        names = [name for name in alternatives if not any(c in name for c in '*?[')]
        package = next((name for name in names if candidate(name)), None)
        if package:
            available.append(package)
        else:
            unavailable.append(alternatives)
    return sorted(set(available)), unavailable


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', action='append', choices=DEVICES,
                        help='repeat for selected devices; default: all ten')
    args = parser.parse_args(argv)
    devices = args.device or list(DEVICES)
    try:
        output = subprocess.run(
            ['dpkg-query', '-W', '-f=${binary:Package}\t${db:Status-Status}\n'],
            capture_output=True, text=True, check=True,
        ).stdout
        missing = missing_requirements(requirements(devices), parse_installed(output))
        available, unavailable = install_packages(missing, apt_candidate)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f'Cannot check Ubuntu package state: {error}', file=sys.stderr)
        return 2

    print('Native-build package check: ' + ', '.join(devices))
    if missing:
        print('Missing recommended system packages:')
        for alternatives in missing:
            print('  ' + ' or '.join(alternatives))
        if available:
            print('\nAsk the user to install (omit sudo in a root shell):')
            print('sudo apt-get update')
            print('sudo apt-get install --no-install-recommends ' + shlex.join(available))
        for alternatives in unavailable:
            print('No candidate in local apt metadata: ' + ' or '.join(alternatives))
        print('If apt metadata is stale, refresh it and rerun this check.')
    else:
        print('Recommended system packages are installed.')

    missing_qemu = any(device.startswith('ledger') for device in devices) and not shutil.which('qemu-arm-static')
    if missing_qemu:
        print('Ledger also needs qemu-arm-static in PATH; check the distro QEMU packaging.')
    print('Still verify branch-specific compiler/CMake/protoc/Rust versions, SDKs,')
    print('Python environments, and runtime libraries. Local toolchains may satisfy')
    print('a missing distro package; use the documented fallback when appropriate.')
    return 1 if missing or missing_qemu else 0


if __name__ == '__main__':
    sys.exit(main())
