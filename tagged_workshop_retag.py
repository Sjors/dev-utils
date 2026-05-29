#!/usr/bin/env python3
"""Retag a linear tagged-workshop branch.

Maps a comma-separated tag list onto first-parent commits in order after a base
commit. Optionally maps a first tag onto the base commit itself and verifies a
hardcoded CI workflow mentions the same step tags. The script updates local tags
only; it prints an explicit push command.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


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


def check_ci_workflow(path: Path, expected_tags: list[str]) -> None:
    text = path.read_text()
    pattern = re.compile(r"(?<![\w.-])step\.\d+(?:\.solution)?(?![\w.-])")
    seen = []
    for match in pattern.finditer(text):
        tag = match.group(0)
        if tag not in seen:
            seen.append(tag)

    if seen != expected_tags:
        sys.stderr.write(
            f"{path} step tag list does not match planned tags\n"
            f"  expected: {', '.join(expected_tags)}\n"
            f"  found:    {', '.join(seen)}\n"
        )
        raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", required=True, help="Workshop branch to scan")
    parser.add_argument("--base", required=True, help="Base ref before step commits")
    parser.add_argument(
        "--tags",
        required=True,
        help="Comma-separated tags in first-parent commit order after --base",
    )
    parser.add_argument(
        "--base-tag",
        help="Optional tag to point at --base before mapping --tags onto branch commits",
    )
    parser.add_argument(
        "--ci-workflow",
        type=Path,
        help="Optional workflow file whose hardcoded step.* tags must match planned tags",
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

    planned = []
    if args.base_tag:
        planned.append((args.base_tag, git(["rev-parse", args.base])))
    planned.extend(zip(tags, commits))

    planned_tags = [tag for tag, _ in planned]
    if len(planned_tags) != len(set(planned_tags)):
        raise SystemExit("planned tag list contains duplicates")

    if args.ci_workflow:
        check_ci_workflow(args.ci_workflow, planned_tags)

    print("Planned tag updates:")
    for tag, commit in planned:
        subject = git(["show", "-s", "--format=%s", commit])
        print(f"  {tag:18} {commit[:12]} {subject}")

    if not args.dry_run:
        for tag, commit in planned:
            git(["tag", "-f", tag, commit])

    tag_refspecs = " ".join(f"+refs/tags/{tag}:refs/tags/{tag}" for tag in planned_tags)
    print()
    print(
        "Push moved branch and tags with:\n"
        f"  git push --force-with-lease {args.remote} {args.branch} {tag_refspecs}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
