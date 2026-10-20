#!/usr/bin/env python3
"""Make a small, independent Coldcard simulator runtime from a built firmware tree."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def stage(source, destination):
    source = source.resolve()
    destination = destination.absolute()
    binary = source / "unix/coldcard-mpy"
    for required in (source / "unix/simulator.py", binary, source / "shared"):
        if not required.exists():
            raise ValueError(f"missing simulator component: {required}")
    if not os.access(binary, os.X_OK):
        raise ValueError(f"simulator binary is not executable: {binary}")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"destination already exists: {destination}")
    destination = destination.parent.resolve() / destination.name
    if destination.is_relative_to(source):
        raise ValueError("destination must be outside the source tree")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        # Dereference shared's generated-file links and the native binary so
        # the runtime is independent of the original compiler/source paths.
        shutil.copytree(source / "shared", temporary / "shared")
        shutil.copytree(
            source / "unix", temporary / "unix",
            ignore=shutil.ignore_patterns("work", "coldcard-mpy", "l-port", "l-mpy", "__pycache__", ".git"),
        )
        shutil.copy2(binary, temporary / "unix/coldcard-mpy")
        for name in ("MicroSD", "VirtDisk", "debug", "settings"):
            (temporary / "unix/work" / name).mkdir(parents=True)
        metadata = source / "hwi-runtime.json"
        if metadata.is_file():
            provenance = json.loads(metadata.read_text())
        else:
            git_root = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "--show-toplevel"], text=True,
            ).strip()
            if Path(git_root).resolve() != source:
                raise ValueError("source needs its own Git checkout or hwi-runtime.json provenance")
            commit = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
            ).strip()
            diff = subprocess.check_output(["git", "-C", str(source), "diff", "--binary", "HEAD"])
            provenance = {"commit": commit, "diff_sha256": hashlib.sha256(diff).hexdigest()}
        provenance["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
        (temporary / "hwi-runtime.json").write_text(json.dumps(provenance, indent=2) + "\n")
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="built firmware or previously staged runtime")
    parser.add_argument("destination", type=Path, help="new directory; existing paths are refused")
    args = parser.parse_args()
    try:
        stage(args.source, args.destination)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")
    print(args.destination.absolute() / "unix/simulator.py")


if __name__ == "__main__":
    main()
