#!/usr/bin/env python3
"""Retag a linear tagged-workshop branch.

Maps a comma-separated tag list onto first-parent commits in order after a base
commit. The script updates local tags only; it prints an explicit push command.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def git(args: list[str], check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", required=True, help="Workshop branch to scan")
    parser.add_argument("--base", required=True, help="Base ref before step commits")
    parser.add_argument(
        "--tags",
        required=True,
        help="Comma-separated tags in first-parent commit order",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Remote name for the printed push command",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned updates without changing tags",
    )
    args = parser.parse_args()

    tags = [tag.strip() for tag in args.tags.split(",") if tag.strip()]
    if not tags:
        raise SystemExit("--tags did not contain any tag names")

    rev_range = f"{args.base}..{args.branch}"
    commits = git(["rev-list", "--reverse", "--first-parent", rev_range]).splitlines()
    if len(commits) != len(tags):
        raise SystemExit(
            f"tag count ({len(tags)}) does not match commit count "
            f"({len(commits)}) in {rev_range}"
        )

    print("Planned tag updates:")
    for tag, commit in zip(tags, commits):
        subject = git(["show", "-s", "--format=%s", commit])
        print(f"  {tag:18} {commit[:12]} {subject}")

    if not args.dry_run:
        for tag, commit in zip(tags, commits):
            git(["tag", "-f", tag, commit])

    refspecs = " ".join(f"+refs/tags/{tag}:refs/tags/{tag}" for tag in tags)
    print()
    print(f"Push moved tags with:\n  git push {args.remote} {refspecs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
