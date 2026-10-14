"""Integration tests using disposable repositories and a disposable PGP key."""

import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "git-fetch-sign"


@unittest.skipUnless(shutil.which("gpg"), "GnuPG is required")
class FetchSignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix="fetch-sign-key-")
        cls.addClassCleanup(cls.keys.cleanup)
        os.chmod(cls.keys.name, 0o700)
        cls.env = {**os.environ, "GNUPGHOME": cls.keys.name,
                   "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        result = subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                        "--quick-generate-key", "Fetch Sign Test <test@example.com>",
                        "ed25519", "sign", "0"], env=cls.env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError(result.stderr.decode())

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=cls.env, check=False)
        cls.keys.cleanup()

    def setUp(self):
        self.env = dict(type(self).env)
        self.tmp = tempfile.TemporaryDirectory(prefix="fetch-sign-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.local = self.root / "local"
        self.git("init", "-b", "main", str(self.source), cwd=self.root)
        self.configure(self.source)
        (self.source / "tracked").write_text("original\n")
        (self.source / "other").write_text("original\n")
        self.git("add", "tracked", "other", cwd=self.source)
        self.git("commit", "--allow-empty", "-m", "base", cwd=self.source)
        self.base = self.git("rev-parse", "HEAD", cwd=self.source).strip()
        self.git("clone", str(self.source), str(self.local), cwd=self.root)
        self.configure(self.local)
        self.git("remote", "add", "workstation", str(self.source))
        self.git("switch", "-c", "topic")
        self.git("commit", "--allow-empty", "-m", "old local tip")
        self.previous = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "main")
        self.git("switch", "-c", "topic", cwd=self.source)
        (self.source / "file").write_text("remote content\n")
        self.git("add", "file", cwd=self.source)
        self.git("commit", "-m", "remote commit\n\nA multiline message.", cwd=self.source)
        self.git("commit", "--allow-empty", "-m", "empty commit", cwd=self.source)

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd or self.local,
                                       env=self.env, stderr=subprocess.PIPE).decode()

    def configure(self, path):
        for key, value in (("user.name", "Test"), ("user.email", "test@example.com"),
                           ("user.signingkey", "test@example.com"), ("commit.gpgsign", "false")):
            self.git("config", key, value, cwd=path)

    def run_script(self, *args, ok=True, cwd=None, remote="workstation"):
        self.source_before = self.git("rev-parse", "topic", cwd=self.source).strip()
        command = [sys.executable, str(SCRIPT), *args]
        if remote is not None:
            command.extend(["--remote", remote])
        result = subprocess.run(command, cwd=cwd or self.local,
                                env=self.env, text=True, capture_output=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def mock_gh(self, branch="topic", code=0):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        mock = bin_dir / "gh"
        self.gh_log = self.root / "gh-args.json"
        mock.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(self.gh_log)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            f"print({branch!r})\n"
            f"sys.exit({code})\n"
        )
        mock.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]

    def assert_signed_history(self):
        old = self.git("rev-list", "--reverse", "--topo-order", f"{self.base}..{self.source_before}",
                       cwd=self.source).splitlines()
        new = self.git("rev-list", "--reverse", "--topo-order", f"{self.base}..topic").splitlines()
        self.assertEqual(len(old), len(new))
        mapping = dict(zip(old, new))
        for before, after in zip(old, new):
            self.git("verify-commit", after)
            fmt = "%T%n%an%n%ae%n%at%n%cn%n%ce%n%ct%n%B"
            self.assertEqual(self.git("show", "-s", f"--format={fmt}", before, cwd=self.source),
                             self.git("show", "-s", f"--format={fmt}", after))
            parents = self.git("show", "-s", "--format=%P", before, cwd=self.source).strip().split()
            self.assertEqual([mapping.get(p, p) for p in parents],
                             self.git("show", "-s", "--format=%P", after).strip().split())
        refs = self.git("for-each-ref", "--format=%(objectname) %(refname)", "refs/fetch-sign")
        self.assertIn(self.previous, refs)
        self.assertIn(self.source_before, refs)

    def test_linked_worktree_and_dirty_unrelated_checkout(self):
        linked = self.root / "linked"
        self.git("worktree", "add", str(linked), "topic")
        (self.local / "unrelated").write_text("leave this alone")
        self.run_script(cwd=linked)
        self.assert_signed_history()
        self.assertEqual((linked / "file").read_text(), "remote content\n")
        self.assertEqual((self.local / "unrelated").read_text(), "leave this alone")
        self.assertEqual(self.git("status", "--porcelain", cwd=linked), "")

    def test_merge_topology(self):
        self.git("switch", "topic")
        self.git("switch", "-c", "side", self.base, cwd=self.source)
        self.git("commit", "--allow-empty", "-m", "side commit", cwd=self.source)
        self.git("switch", "topic", cwd=self.source)
        self.git("merge", "--no-ff", "side", "-m", "merge side", cwd=self.source)
        self.run_script("topic")
        self.assert_signed_history()

    def upstream_master_merge(self):
        # Switch the upstream default from main to master after the clone, so
        # origin/HEAD is stale and origin/master is initially missing locally.
        self.git("switch", "main", cwd=self.source)
        self.git("branch", "-m", "master", cwd=self.source)
        self.git("commit", "--allow-empty", "-m", "new upstream commit", cwd=self.source)
        upstream = self.git("rev-parse", "HEAD", cwd=self.source).strip()
        self.git("switch", "topic", cwd=self.source)
        self.git("merge", "--no-ff", "master", "-m", "merge upstream master", cwd=self.source)
        self.git("branch", "master", self.base)
        self.git("switch", "topic")
        return upstream

    def assert_upstream_preserved(self, upstream):
        self.assertEqual(self.git("rev-parse", "origin/master").strip(), upstream)
        self.assertEqual(self.git("rev-parse", "master").strip(), self.base)
        self.git("merge-base", "--is-ancestor", upstream, "topic")
        original = self.git("rev-list", "--reverse", "--topo-order", self.source_before, f"^{upstream}",
                            cwd=self.source).splitlines()
        signed = self.git("rev-list", "--reverse", "--topo-order", "topic", f"^{upstream}").splitlines()
        self.assertEqual(len(original), 3)
        self.assertEqual(len(signed), 3)
        mapping = dict(zip(original, signed))
        for before, after in zip(original, signed):
            self.git("verify-commit", after)
            parents = self.git("show", "-s", "--format=%P", before, cwd=self.source).strip().split()
            self.assertEqual([mapping.get(p, p) for p in parents],
                             self.git("show", "-s", "--format=%P", after).strip().split())
        self.assertEqual(self.git("show", "-s", "--format=%G?", upstream).strip(), "N")
        self.assertEqual(self.git("rev-parse", "topic^{tree}").strip(),
                         self.git("rev-parse", "topic^{tree}", cwd=self.source).strip())

    def test_fetches_missing_master_and_preserves_merged_upstream_commits(self):
        upstream = self.upstream_master_merge()
        result = self.run_script()
        self.assertIn("3 commit(s)", result.stdout)
        self.assert_upstream_preserved(upstream)

    def test_refreshes_stale_master_even_with_explicit_old_base(self):
        upstream = self.upstream_master_merge()
        self.git("update-ref", "refs/remotes/origin/master", self.base)
        self.run_script("--base", self.base)
        self.assert_upstream_preserved(upstream)

    def test_mainline_only_branch_synced_without_signing(self):
        self.git("switch", "main")
        self.git("switch", "main", cwd=self.source)
        self.git("commit", "--allow-empty", "-m", "upstream change", cwd=self.source)
        source = self.git("rev-parse", "HEAD", cwd=self.source).strip()
        self.git("config", "gpg.program", "/usr/bin/false")
        result = self.run_script()
        self.assertIn("0 commit(s)", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), source)
        self.assertEqual(self.git("show", "-s", "--format=%G?", "HEAD").strip(), "N")

    def test_explicit_upstream(self):
        self.git("switch", "topic")
        self.git("remote", "rename", "origin", "upstream")
        self.run_script("--upstream", "upstream/main")
        self.assert_signed_history()

    def test_upstream_fetch_failure_leaves_dirty_worktree_unchanged(self):
        self.git("switch", "topic")
        (self.local / "tracked").write_text("local edits\n")
        self.git("remote", "set-url", "origin", str(self.root / "missing-remote"))
        result = self.run_script(ok=False)
        self.assertIn("does not appear to be a git repository", result.stderr)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual((self.local / "tracked").read_text(), "local edits\n")
        self.assertEqual(self.git("stash", "list"), "")

    def test_pushes_signed_tip_to_checked_out_remote_branch(self):
        self.git("switch", "topic")
        config = self.git("config", "--local", "--list", cwd=self.source)
        result = self.run_script()
        self.assertIn("Pushed signed branch to workstation/topic", result.stdout)
        self.assert_signed_history()
        self.assertEqual(self.git("rev-parse", "topic").strip(),
                         self.git("rev-parse", "HEAD", cwd=self.source).strip())
        self.assertEqual(self.git("symbolic-ref", "HEAD", cwd=self.source).strip(), "refs/heads/topic")
        self.assertEqual(self.git("status", "--porcelain", cwd=self.source), "")
        self.assertEqual(self.git("config", "--local", "--list", cwd=self.source), config)

    def test_push_updates_remote_linked_worktree(self):
        linked = self.root / "remote linked"
        self.git("switch", "main", cwd=self.source)
        self.git("worktree", "add", str(linked), "topic", cwd=self.source)
        (self.source / "unrelated edits").write_text("preserve remote primary worktree")
        self.git("switch", "topic")
        self.run_script()
        self.assert_signed_history()
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=linked).strip(),
                         self.git("rev-parse", "topic").strip())
        self.assertEqual(self.git("symbolic-ref", "HEAD", cwd=linked).strip(), "refs/heads/topic")
        self.assertEqual(self.git("status", "--porcelain", cwd=linked), "")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.source).strip(), self.base)
        self.assertEqual((self.source / "unrelated edits").read_text(), "preserve remote primary worktree")

    def test_dirty_remote_refuses_push_and_preserves_local_signed_tip(self):
        self.git("switch", "topic")
        remote_tip = self.git("rev-parse", "HEAD", cwd=self.source).strip()
        (self.source / "tracked").write_text("remote edits\n")
        self.git("add", "tracked", cwd=self.source)
        (self.source / "tracked").write_text("remote unstaged edits\n")
        status = self.git("status", "--porcelain", "-z", cwd=self.source)
        result = self.run_script(ok=False)
        self.assertIn("Local sync succeeded", result.stderr)
        self.assertIn("push to workstation failed", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.source).strip(), remote_tip)
        self.assertEqual(self.git("status", "--porcelain", "-z", cwd=self.source), status)
        self.assertEqual((self.source / "tracked").read_text(), "remote unstaged edits\n")
        self.assertEqual(self.git("show", ":tracked", cwd=self.source), "remote edits\n")
        self.assert_signed_history()

    def test_no_push_keeps_remote_tip_unchanged(self):
        self.git("switch", "topic")
        result = self.run_script("--no-push")
        self.assertIn("No commits pushed", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.source).strip(), self.source_before)
        self.assert_signed_history()

    def test_concurrent_remote_commit_is_not_overwritten(self):
        self.git("switch", "topic")
        hook = self.local / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\n" +
                        f"git -C {shlex.quote(str(self.source))} commit --allow-empty -m 'concurrent remote commit'\n")
        hook.chmod(0o755)
        result = self.run_script(ok=False)
        self.assertIn("push to workstation failed", result.stderr)
        self.assertNotEqual(self.git("rev-parse", "HEAD", cwd=self.source).strip(), self.source_before)
        self.assertEqual(self.git("show", "-s", "--format=%s", "HEAD", cwd=self.source).strip(),
                         "concurrent remote commit")
        self.assert_signed_history()

    def test_dirty_target_stashed_with_index_and_older_stash_preserved(self):
        self.git("switch", "topic")
        (self.local / "other").write_text("older stash\n")
        self.git("stash", "push", "-m", "older unrelated stash")
        older_stash = self.git("rev-parse", "refs/stash").strip()
        (self.local / "tracked").write_text("staged\n")
        self.git("add", "tracked")
        (self.local / "tracked").write_text("unstaged\n")
        (self.local / "untracked").write_text("keep")
        (self.local / "untracked link").symlink_to("untracked")
        (self.local / "ignored").write_text("ignored content")
        self.git("config", "core.excludesFile", str(self.root / "ignore"))
        (self.root / "ignore").write_text("ignored\n")
        status = self.git("status", "--porcelain", "-z")
        index = self.git("diff", "--cached", "--binary")
        edits = self.git("diff", "--binary")
        result = self.run_script("topic")
        self.assertIn("Restored stashed", result.stdout)
        self.assert_signed_history()
        self.assertEqual(self.git("status", "--porcelain", "-z"), status)
        self.assertEqual(self.git("diff", "--cached", "--binary"), index)
        self.assertEqual(self.git("diff", "--binary"), edits)
        self.assertEqual((self.local / "untracked").read_text(), "keep")
        self.assertEqual(os.readlink(self.local / "untracked link"), "untracked")
        self.assertEqual((self.local / "ignored").read_text(), "ignored content")
        self.assertEqual(self.git("rev-parse", "refs/stash").strip(), older_stash)
        self.assertEqual(len(self.git("stash", "list").splitlines()), 1)

    def test_conflicting_stash_reverts_sync_and_restores_changes(self):
        self.git("switch", "topic")
        (self.source / "tracked").write_text("remote changes\n")
        self.git("commit", "-am", "conflicting remote changes", cwd=self.source)
        (self.local / "tracked").write_text("local changes\n")
        (self.local / "other").write_text("staged changes\n")
        self.git("add", "other")
        (self.local / "notes").write_text("untracked notes")
        (self.local / "notes link").symlink_to("notes")
        status = self.git("status", "--porcelain", "-z")
        index = self.git("diff", "--cached", "--binary")
        edits = self.git("diff", "--binary")
        result = self.run_script("topic", ok=False)
        self.assertIn("sync reverted and original worktree changes restored", result.stderr)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual(self.git("status", "--porcelain", "-z"), status)
        self.assertEqual(self.git("diff", "--cached", "--binary"), index)
        self.assertEqual(self.git("diff", "--binary"), edits)
        self.assertEqual((self.local / "notes").read_text(), "untracked notes")
        self.assertEqual(os.readlink(self.local / "notes link"), "notes")
        self.assertEqual(self.git("stash", "list"), "")

    def test_untracked_collision_reverts_sync(self):
        self.git("switch", "topic")
        (self.local / "file").write_text("local untracked file")
        (self.local / "notes").write_text("local notes")
        status = self.git("status", "--porcelain", "-z")
        result = self.run_script("topic", ok=False)
        self.assertIn("sync reverted", result.stderr)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual(self.git("status", "--porcelain", "-z"), status)
        self.assertEqual((self.local / "file").read_text(), "local untracked file")
        self.assertEqual((self.local / "notes").read_text(), "local notes")
        self.assertEqual(self.git("stash", "list"), "")

    def test_dirty_dry_run_does_not_stash(self):
        self.git("switch", "topic")
        (self.local / "tracked").write_text("local changes\n")
        (self.local / "notes").write_text("local notes")
        status = self.git("status", "--porcelain", "-z")
        self.run_script("--dry-run")
        self.assertEqual(self.git("status", "--porcelain", "-z"), status)
        self.assertEqual(self.git("stash", "list"), "")
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)

    def test_unresolved_conflicts_still_refused(self):
        self.git("switch", "topic")
        blob = self.git("hash-object", "tracked").strip()
        subprocess.run(["git", "update-index", "--index-info"], cwd=self.local,
                       input=f"0 {'0' * 40}\ttracked\n100644 {blob} 1\ttracked\n".encode(),
                       env=self.env, check=True)
        result = self.run_script(ok=False)
        self.assertIn("Unresolved conflicts", result.stderr)
        self.assertEqual(self.git("stash", "list"), "")

    def test_failed_rollback_preserves_recovery_stash(self):
        self.git("switch", "topic")
        (self.local / "file").write_text("original untracked content")
        real_git = shutil.which("git", path=self.env["PATH"])
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        counter = self.root / "stash-apply-count"
        wrapper = bin_dir / "git"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "if 'stash' in args and args[args.index('stash') + 1] == 'apply':\n"
            f"    counter = pathlib.Path({str(counter)!r})\n"
            "    count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
            "    counter.write_text(str(count))\n"
            "    if count == 2:\n"
            "        print('simulated restore failure', file=sys.stderr)\n"
            "        sys.exit(1)\n"
            f"os.execv({real_git!r}, ['git', *args])\n"
        )
        wrapper.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]
        result = self.run_script(ok=False)
        self.assertIn("recovery failed", result.stderr)
        self.assertIn("Saved changes retained", result.stderr)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        stash = self.git("rev-parse", "refs/stash").strip()
        self.assertIn(stash, self.git("for-each-ref", "refs/fetch-sign"))
        self.assertEqual(self.git("show", f"{stash}^3:file"), "original untracked content")

    def test_signing_failure_preserves_branch_and_checkout(self):
        self.git("switch", "topic")
        (self.local / "tracked").write_text("local changes\n")
        (self.local / "notes").write_text("local notes")
        status = self.git("status", "--porcelain", "-z")
        self.git("config", "gpg.program", "/usr/bin/false")
        self.run_script("topic", ok=False)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual(self.git("status", "--porcelain", "-z"), status)
        self.assertEqual(self.git("stash", "list"), "")

    def test_dry_run(self):
        self.git("switch", "topic")
        result = self.run_script("--dry-run", "topic")
        self.assertIn("2 commit(s)", result.stdout)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual(self.git("for-each-ref", "refs/fetch-sign"), "")

    def test_new_local_branch(self):
        self.git("branch", "-D", "topic")
        linked = self.root / "new linked"
        self.run_script("topic", "--worktree", str(linked))
        self.git("verify-commit", "topic")
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD", cwd=linked).strip(), "topic")
        self.assertEqual((linked / "file").read_text(), "remote content\n")

    def test_missing_worktree_requires_option_before_fetch(self):
        result = self.run_script("topic", ok=False)
        self.assertIn("--worktree PATH", result.stderr)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual(self.git("for-each-ref", "refs/fetch-sign"), "")

    def test_creates_worktree_for_existing_branch(self):
        linked = self.root / "linked"
        self.run_script("topic", "--worktree", str(linked))
        self.assert_signed_history()
        self.assertEqual(self.git("status", "--porcelain", cwd=linked), "")
        self.assertEqual((linked / "file").read_text(), "remote content\n")

    def test_dry_run_does_not_create_worktree(self):
        linked = self.root / "linked"
        self.run_script("--dry-run", "topic", "--worktree", str(linked))
        self.assertFalse(linked.exists())
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)

    def test_signing_failure_does_not_create_worktree(self):
        linked = self.root / "linked"
        self.git("config", "gpg.program", "/usr/bin/false")
        self.run_script("topic", "--worktree", str(linked), ok=False)
        self.assertFalse(linked.exists())
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)

    def test_signing_failure_does_not_create_new_branch(self):
        self.git("branch", "-D", "topic")
        self.git("config", "gpg.program", "/usr/bin/false")
        linked = self.root / "linked"
        self.run_script("topic", "--worktree", str(linked), ok=False)
        self.assertFalse(linked.exists())
        self.assertEqual(self.git("for-each-ref", "refs/heads/topic"), "")

    def test_occupied_destination_refused(self):
        linked = self.root / "linked"
        linked.mkdir()
        (linked / "keep").write_text("keep")
        result = self.run_script("topic", "--worktree", str(linked), ok=False)
        self.assertIn("already exists", result.stderr)
        self.assertEqual((linked / "keep").read_text(), "keep")
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)

    def test_existing_worktree_accepts_matching_path(self):
        self.git("switch", "topic")
        self.run_script("topic", "--worktree", str(self.local))
        self.assert_signed_history()

    def test_existing_worktree_rejects_different_path(self):
        self.git("switch", "topic")
        self.run_script("topic", "--worktree", str(self.root / "other"), ok=False)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)

    def test_pr_number_and_repo_resolve_branch(self):
        self.mock_gh()
        self.git("switch", "topic")
        self.run_script("--pr", "123", "--repo", "owner/repo")
        self.assert_signed_history()
        self.assertEqual(json.loads(self.gh_log.read_text()),
                         ["pr", "view", "123", "--json", "headRefName", "--jq",
                          ".headRefName", "--repo", "owner/repo"])

    def test_pr_url_creates_missing_worktree(self):
        self.mock_gh()
        linked = self.root / "linked"
        url = "https://github.com/owner/repo/pull/123"
        self.run_script("--pr", url, "--worktree", str(linked))
        self.assert_signed_history()
        self.assertIn(url, json.loads(self.gh_log.read_text()))

    def test_pr_lookup_failure_leaves_branch_unchanged(self):
        self.mock_gh(code=1)
        self.run_script("--pr", "123", ok=False)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual(self.git("for-each-ref", "refs/fetch-sign"), "")

    def test_pr_invalid_head_branch_refused(self):
        self.mock_gh(branch="../invalid")
        self.run_script("--pr", "123", ok=False)
        self.assertEqual(self.git("for-each-ref", "refs/fetch-sign"), "")

    def test_pr_lookup_without_head_branch_refused(self):
        self.mock_gh(branch="null")
        result = self.run_script("--pr", "123", ok=False)
        self.assertIn("no head branch", result.stderr)
        self.assertEqual(self.git("for-each-ref", "refs/fetch-sign"), "")

    def test_branch_and_pr_mutually_exclusive(self):
        self.run_script("topic", "--pr", "123", ok=False)

    def test_defaults_to_current_branch(self):
        self.git("switch", "topic")
        self.run_script()
        self.assert_signed_history()

    def test_detached_head_requires_explicit_branch_or_pr(self):
        self.git("switch", "--detach")
        result = self.run_script(ok=False)
        self.assertIn("Detached HEAD; pass BRANCH or --pr", result.stderr)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual(self.git("for-each-ref", "refs/fetch-sign"), "")

    def test_detached_head_accepts_explicit_branch(self):
        linked = self.root / "linked"
        self.git("worktree", "add", str(linked), "topic")
        self.git("switch", "--detach")
        self.run_script("topic")
        self.assert_signed_history()

    def test_missing_remote_branch(self):
        result = self.run_script("missing", "--worktree", str(self.root / "missing"), ok=False)
        self.assertIn("remote ref", result.stderr)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)

    def test_remote_argument_required_before_fetching(self):
        result = self.run_script(ok=False, remote=None)
        self.assertIn("required: --remote", result.stderr)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual(self.git("for-each-ref", "refs/fetch-sign"), "")

    def test_arbitrary_remote_name(self):
        self.git("remote", "rename", "workstation", "builder")
        self.git("switch", "topic")
        result = self.run_script(remote="builder")
        self.assertIn("Pushed signed branch to builder/topic", result.stdout)
        self.assert_signed_history()
        self.assertEqual(self.git("rev-parse", "topic").strip(),
                         self.git("rev-parse", "topic", cwd=self.source).strip())

    def test_unconfigured_remote_refused(self):
        result = self.run_script(ok=False, remote="missing")
        self.assertIn("No such remote", result.stderr)
        self.assertEqual(self.git("for-each-ref", "refs/fetch-sign"), "")


if __name__ == "__main__":
    unittest.main()
