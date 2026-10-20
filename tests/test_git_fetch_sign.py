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
        cls.contributor_keys = tempfile.TemporaryDirectory(prefix="fetch-sign-contributor-")
        cls.addClassCleanup(cls.contributor_keys.cleanup)
        os.chmod(cls.contributor_keys.name, 0o700)
        cls.contributor_env = {**cls.env, "GNUPGHOME": cls.contributor_keys.name}
        cls.addClassCleanup(subprocess.run, ["gpgconf", "--kill", "gpg-agent"],
                            env=cls.contributor_env, check=False)
        result = subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                        "--quick-generate-key", "Contributor <contributor@example.com>",
                        "ed25519", "sign", "0"], env=cls.contributor_env,
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

    def git(self, *args, cwd=None, env=None):
        return subprocess.check_output(["git", *args], cwd=cwd or self.local,
                                       env=env or self.env, stderr=subprocess.PIPE).decode()

    def configure(self, path):
        for key, value in (("user.name", "Test"), ("user.email", "test@example.com"),
                           ("user.signingkey", "test@example.com"), ("commit.gpgsign", "false")):
            self.git("config", key, value, cwd=path)

    def run_script(self, *args, ok=True, cwd=None, remote="workstation", defaults=False):
        self.source_before = self.git("rev-parse", "topic", cwd=self.source).strip()
        command = [sys.executable, str(SCRIPT), *args]
        # Existing signing scenarios deliberately replace divergent history.
        if not defaults:
            command.extend(["--allow-dirty", "--allow-unpushed"])
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

    def mock_ssh_remote(self, *, path="/home/remote/repository-pr/topic", branch="topic",
                        home="/home/remote", url="remote:~/repository", fail=False):
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        self.ssh_log = self.root / "ssh-lookup.json"
        self.local_home = self.root / "local home"
        tip = self.git("rev-parse", "topic", cwd=self.source).strip()
        listing = (f"worktree {path}\0HEAD {tip}\0branch refs/heads/{branch}\0\0").encode()
        payload = home.encode() + b"\0" + listing
        mock = bin_dir / "ssh"
        mock.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, shlex, sys\n"
            "command = sys.argv[-1]\n"
            "if 'worktree list --porcelain -z' in command:\n"
            f"    pathlib.Path({str(self.ssh_log)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            f"    sys.stdout.buffer.write({payload!r})\n"
            f"    sys.exit({1 if fail else 0})\n"
            "args = shlex.split(command)\n"
            f"args[-1] = {str(self.source)!r}\n"
            "os.execvp(args[0], args)\n"
        )
        mock.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]
        self.env["GIT_SSH_VARIANT"] = "ssh"
        self.env["HOME"] = str(self.local_home)
        self.git("remote", "set-url", "workstation", url)
        return self.local_home / "repository-pr" / "topic"

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

    def test_merge_topology_with_explicit_base(self):
        self.git("switch", "topic")
        self.git("switch", "-c", "side", self.base, cwd=self.source)
        self.git("commit", "--allow-empty", "-m", "side commit", cwd=self.source)
        self.git("switch", "topic", cwd=self.source)
        self.git("merge", "--no-ff", "side", "-m", "merge side", cwd=self.source)
        self.run_script("topic", "--base", self.base)
        self.assert_signed_history()

    def dependency_merges(self):
        for name in ("dependency-one", "dependency-two"):
            self.git("switch", "-c", name, "main", cwd=self.source)
            self.git("commit", "--allow-empty", "--author=Contributor <contributor@example.com>",
                     "-m", name, cwd=self.source)
            self.git("switch", "topic", cwd=self.source)
            self.git("merge", "--no-ff", name, "-m", f"Merge {name}", cwd=self.source)
        self.git("switch", "topic")
        return self.git("rev-parse", "HEAD", cwd=self.source).strip()

    def assert_merge_tail(self, merge, count):
        self.git("merge-base", "--is-ancestor", merge, "topic")
        self.assertEqual(self.git("show", "-s", "--format=%G?", merge).strip(), "N")
        tail = self.git("rev-list", "topic", f"^{merge}", "^refs/remotes/origin/main").splitlines()
        self.assertEqual(len(tail), count)
        for commit in tail:
            self.git("verify-commit", commit)
        self.assertEqual(self.git("rev-parse", "topic^{tree}"),
                         self.git("rev-parse", "topic^{tree}", cwd=self.source))

    def test_dependency_merges_preserved_and_only_tail_signed(self):
        merge = self.dependency_merges()
        for name in ("my first change", "my second change"):
            self.git("commit", "--allow-empty", "-m", name, cwd=self.source)
        result = self.run_script()
        self.assertIn(f"Protected merged dependencies through {merge}", result.stdout)
        self.assertIn("2 commit(s)", result.stdout)
        self.assertEqual(self.git("rev-parse", "topic~2").strip(), merge)
        self.assert_merge_tail(merge, 2)

    def test_dependency_merge_at_tip_needs_no_signing_key(self):
        merge = self.dependency_merges()
        self.git("config", "gpg.program", "/usr/bin/false")
        result = self.run_script()
        self.assertIn("0 commit(s)", result.stdout)
        self.assertEqual(self.git("rev-parse", "topic").strip(), merge)
        self.assert_merge_tail(merge, 0)

    def test_later_upstream_merge_does_not_hide_topic_tail(self):
        merge = self.dependency_merges()
        self.git("commit", "--allow-empty", "-m", "my change", cwd=self.source)
        self.git("switch", "main", cwd=self.source)
        self.git("commit", "--allow-empty", "-m", "upstream change", cwd=self.source)
        upstream = self.git("rev-parse", "HEAD", cwd=self.source).strip()
        self.git("switch", "topic", cwd=self.source)
        self.git("merge", "--no-ff", "main", "-m", "Merge upstream", cwd=self.source)
        result = self.run_script()
        self.assertIn(f"Protected merged dependencies through {merge}", result.stdout)
        self.assertIn("2 commit(s)", result.stdout)
        self.assertEqual(self.git("rev-parse", "topic^2").strip(), upstream)
        self.assert_merge_tail(merge, 2)

    def test_dependency_merge_dry_run_reports_only_tail(self):
        merge = self.dependency_merges()
        self.git("commit", "--allow-empty", "-m", "my change", cwd=self.source)
        source = self.git("rev-parse", "HEAD", cwd=self.source)
        self.git("config", "gpg.program", "/usr/bin/false")
        result = self.run_script("--dry-run")
        self.assertIn(f"Protected merged dependencies through {merge}", result.stdout)
        self.assertIn("1 commit(s)", result.stdout)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual(self.git("rev-parse", "topic", cwd=self.source), source)

    def test_published_parent_above_merge_extends_protection(self):
        merge = self.dependency_merges()
        self.git("commit", "--allow-empty", "--author=Contributor <contributor@example.com>",
                 "-m", "published parent change", cwd=self.source)
        parent = self.git("rev-parse", "HEAD", cwd=self.source).strip()
        publisher = self.root / "publisher"
        self.git("clone", "--bare", str(self.source), str(publisher), cwd=self.root)
        self.git("branch", "-m", "topic", "parent", cwd=publisher)
        self.git("remote", "add", "contributor", str(publisher))
        self.git("commit", "--allow-empty", "-m", "my change", cwd=self.source)
        result = self.run_script()
        self.assertIn(f"Protected merged dependencies through {merge}", result.stdout)
        self.assertIn(f"Protected stacked base contributor/parent: {parent}", result.stdout)
        self.assertIn("1 commit(s)", result.stdout)
        self.assert_stack_preserved(parent)

    def sign_source_topic(self, contributor=False):
        # Sign only the two topic commits; retain the upstream base unchanged.
        env = self.contributor_env if contributor else self.env
        key = "contributor@example.com" if contributor else "test@example.com"
        self.git("-c", "core.editor=true", "-c", f"user.signingkey={key}",
                 "rebase", "--force-rebase", "--gpg-sign", "main", cwd=self.source,
                 env={**env, "GIT_SEQUENCE_EDITOR": "true", "GIT_EDITOR": "true"})
        return self.git("rev-parse", "topic", cwd=self.source).strip()

    def test_missing_contributor_key_keeps_signed_history(self):
        signed = self.sign_source_topic(contributor=True)
        self.git("switch", "topic")
        self.git("config", "user.signingkey", "missing-signing-key")
        result = self.run_script()
        self.assertIn("Kept signed commit 2/2", result.stdout)
        self.assertNotIn("\nSigned ", result.stdout)
        self.assertEqual(self.git("rev-parse", "topic").strip(), signed)
        self.assertEqual(self.git("rev-parse", "topic", cwd=self.source).strip(), signed)

    def test_log_configuration_does_not_verify_existing_signatures(self):
        signed = self.sign_source_topic(contributor=True)
        self.git("switch", "topic")
        marker = self.root / "gpg-invoked"
        wrapper = self.root / "gpg-fail"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import pathlib, sys\n"
            f"pathlib.Path({str(marker)!r}).touch()\n"
            "sys.exit(1)\n"
        )
        wrapper.chmod(0o755)
        self.git("config", "gpg.program", str(wrapper))
        self.git("config", "log.showSignature", "true")
        self.run_script()
        self.assertFalse(marker.exists(), "Existing signatures were verified")
        self.assertEqual(self.git("rev-parse", "topic").strip(), signed)

    def test_unsigned_append_keeps_unknown_contributor_signature(self):
        signed = self.sign_source_topic(contributor=True)
        self.git("commit", "--allow-empty", "-m", "new unsigned commit", cwd=self.source)
        self.git("switch", "topic")
        result = self.run_script()
        self.assertIn("Kept signed commit 2/3", result.stdout)
        self.assertIn("Signed 3/3", result.stdout)
        self.assertEqual(self.git("rev-parse", "topic^").strip(), signed)
        self.git("verify-commit", "topic")
        self.assertEqual(self.git("rev-parse", "topic", cwd=self.source),
                         self.git("rev-parse", "topic"))

    def test_signed_payload_resigned_without_contributor_key(self):
        self.git("-c", "user.signingkey=contributor@example.com", "commit", "--amend",
                 "--allow-empty", "--no-edit", "-S", cwd=self.source, env=self.contributor_env)
        signed = self.git("rev-parse", "topic", cwd=self.source).strip()
        self.git("switch", "topic")
        result = self.run_script()
        self.assertIn("Signed 1/2", result.stdout)
        self.assertIn("Signed 2/2", result.stdout)
        self.assertNotEqual(self.git("rev-parse", "topic").strip(), signed)
        self.assert_signed_history()

    def test_already_signed_history_kept_without_signing_key(self):
        signed = self.sign_source_topic()
        self.git("switch", "topic")
        self.git("config", "user.signingkey", "missing-signing-key")
        result = self.run_script()
        self.assertIn("Kept signed commit 2/2", result.stdout)
        self.assertNotIn("\nSigned ", result.stdout)
        self.assertEqual(self.git("rev-parse", "topic").strip(), signed)
        self.assert_signed_history()

    def test_new_commit_keeps_signed_prefix_and_fast_forward_ancestry(self):
        published = self.sign_source_topic()
        self.git("fetch", "origin", "refs/heads/topic:refs/remotes/origin/topic")
        self.git("commit", "--allow-empty", "-m", "new unsigned commit", cwd=self.source)
        self.git("switch", "topic")
        result = self.run_script()
        self.assertIn("Kept signed commit 2/3", result.stdout)
        self.assertIn("Signed 3/3", result.stdout)
        self.assertEqual(self.git("rev-parse", "topic^").strip(), published)
        self.git("merge-base", "--is-ancestor", "origin/topic", "topic")
        self.assertEqual(self.git("rev-list", "--count", "origin/topic..topic").strip(), "1")
        self.assert_signed_history()

    def test_repeated_sync_does_not_rewrite_signed_history(self):
        self.git("switch", "topic")
        self.run_script()
        first = self.git("rev-parse", "topic").strip()
        self.git("config", "user.signingkey", "missing-signing-key")
        self.run_script()
        self.assertEqual(self.git("rev-parse", "topic").strip(), first)
        self.assert_signed_history()

    def test_signed_descendant_is_resigned_when_unsigned_parent_changes(self):
        self.git("commit", "--amend", "--allow-empty", "--no-edit", "-S", cwd=self.source)
        signed = self.git("rev-parse", "topic", cwd=self.source).strip()
        self.git("switch", "topic")
        result = self.run_script()
        self.assertIn("Signed 1/2", result.stdout)
        self.assertIn("Signed 2/2", result.stdout)
        self.assertNotEqual(self.git("rev-parse", "topic").strip(), signed)
        self.assert_signed_history()

    def test_invalid_existing_signature_kept_without_verification(self):
        signed = self.sign_source_topic()
        raw = self.git("cat-file", "commit", signed, cwd=self.source).encode()
        raw = raw.replace(b"empty commit", b"tampered message")
        tampered = subprocess.check_output(["git", "hash-object", "-w", "-t", "commit", "--stdin"],
                                          input=raw, cwd=self.source, env=self.env).decode().strip()
        self.git("reset", "--hard", tampered, cwd=self.source)
        self.git("commit", "--allow-empty", "-m", "new unsigned commit", cwd=self.source)
        self.git("switch", "topic")
        result = self.run_script()
        self.assertIn("Kept signed commit 2/3", result.stdout)
        self.assertIn("Signed 3/3", result.stdout)
        self.assertEqual(self.git("rev-parse", "topic^").strip(), tampered)
        self.git("verify-commit", "topic")
        self.assertEqual(self.git("rev-parse", "topic", cwd=self.source),
                         self.git("rev-parse", "topic"))

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

    def published_stack(self):
        self.git("reset", "--hard", "main", cwd=self.source)
        (self.source / "file").write_text("contributor content\n")
        self.git("add", "file", cwd=self.source)
        for subject in ("contributor change", "contributor follow-up"):
            self.git("-c", "user.name=Contributor", "-c", "user.email=contributor@example.com",
                     "commit", "--allow-empty", "-m", subject, cwd=self.source)
        parent = self.git("rev-parse", "HEAD", cwd=self.source).strip()
        publisher = self.root / "publisher"
        self.git("clone", "--bare", str(self.source), str(publisher), cwd=self.root)
        self.git("branch", "-m", "topic", "parent", cwd=publisher)
        self.git("remote", "add", "contributor", str(publisher))
        self.git("commit", "--allow-empty", "-m", "my stacked change", cwd=self.source)
        self.git("switch", "topic")
        return parent, publisher

    def assert_stack_preserved(self, parent):
        self.assertEqual(self.git("rev-parse", "topic^").strip(), parent)
        self.assertEqual(self.git("rev-parse", "contributor/parent").strip(), parent)
        self.assertEqual(self.git("show", "-s", "--format=%G?", parent).strip(), "N")
        self.git("verify-commit", "topic")
        self.assertEqual(self.git("rev-parse", "topic^{tree}").strip(),
                         self.git("rev-parse", "topic^{tree}", cwd=self.source).strip())

    def test_detects_published_stack_without_local_parent_ref(self):
        parent, _ = self.published_stack()
        self.assertNotIn("contributor/parent", self.git("branch", "-r"))
        result = self.run_script()
        self.assertIn(f"Protected stacked base contributor/parent: {parent}", result.stdout)
        self.assertIn("1 commit(s)", result.stdout)
        self.assert_stack_preserved(parent)

    def test_refreshes_stale_stacked_parent_ref(self):
        parent, _ = self.published_stack()
        self.git("update-ref", "refs/remotes/contributor/parent", self.base)
        self.run_script()
        self.assert_stack_preserved(parent)

    def test_stale_local_tip_alone_does_not_protect_a_stack(self):
        parent, publisher = self.published_stack()
        self.git("fetch", "contributor", "refs/heads/parent:refs/remotes/contributor/parent")
        self.git("update-ref", "refs/heads/parent", self.base, cwd=publisher)
        result = self.run_script()
        self.assertNotIn("Protected stacked base", result.stdout)
        self.assertIn("3 commit(s)", result.stdout)
        self.assert_signed_history()

    def test_foreign_author_without_published_branch_is_signed(self):
        _, publisher = self.published_stack()
        self.git("update-ref", "-d", "refs/heads/parent", cwd=publisher)
        result = self.run_script()
        self.assertNotIn("Protected stacked base", result.stdout)
        self.assertIn("3 commit(s)", result.stdout)
        self.assert_signed_history()

    def test_explicit_base_disables_stack_detection(self):
        self.published_stack()
        result = self.run_script("--base", self.base)
        self.assertNotIn("Protected stacked base", result.stdout)
        self.assertIn("3 commit(s)", result.stdout)
        self.assert_signed_history()

    def test_stacked_dry_run_fetches_parent_without_signing(self):
        parent, _ = self.published_stack()
        source = self.git("rev-parse", "topic", cwd=self.source)
        result = self.run_script("--dry-run")
        self.assertIn("1 commit(s)", result.stdout)
        self.assertEqual(self.git("rev-parse", "contributor/parent").strip(), parent)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual(self.git("rev-parse", "topic", cwd=self.source), source)

    def test_other_authors_branch_without_own_commits_is_not_excluded(self):
        parent, _ = self.published_stack()
        self.git("reset", "--hard", parent, cwd=self.source)
        result = self.run_script()
        self.assertNotIn("Protected stacked base", result.stdout)
        self.assertIn("2 commit(s)", result.stdout)
        self.assert_signed_history()

    def race_parent_fetch(self, publisher, replacement):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        real_git = shutil.which("git")
        wrapper = bin_dir / "git"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import os, subprocess, sys\n"
            "if 'fetch' in sys.argv and 'contributor' in sys.argv:\n"
            f"    subprocess.run([{real_git!r}, '-C', {str(publisher)!r}, 'update-ref',\n"
            f"                    'refs/heads/parent', {replacement!r}], check=True)\n"
            f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n"
        )
        wrapper.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]

    def test_racing_parent_rewrite_refuses_sync(self):
        _, publisher = self.published_stack()
        source = self.git("rev-parse", "topic", cwd=self.source)
        self.race_parent_fetch(publisher, self.base)
        result = self.run_script(ok=False)
        self.assertIn("Stacked base changed", result.stderr)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual(self.git("rev-parse", "topic", cwd=self.source), source)

    def test_racing_parent_fast_forward_still_signs_own_commit(self):
        parent, publisher = self.published_stack()
        advanced = self.git("rev-parse", "topic", cwd=self.source).strip()
        self.git("fetch", str(self.source), "topic:refs/heads/advanced", cwd=publisher)
        self.race_parent_fetch(publisher, advanced)
        result = self.run_script()
        self.assertIn("1 commit(s)", result.stdout)
        self.assertEqual(self.git("rev-parse", "contributor/parent").strip(), advanced)
        self.assertEqual(self.git("rev-parse", "topic^").strip(), parent)
        self.git("verify-commit", "topic")

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

    def test_dirty_remote_refuses_push_and_rolls_back_local_tip(self):
        self.git("switch", "topic")
        remote_tip = self.git("rev-parse", "HEAD", cwd=self.source).strip()
        (self.source / "tracked").write_text("remote edits\n")
        self.git("add", "tracked", cwd=self.source)
        (self.source / "tracked").write_text("remote unstaged edits\n")
        status = self.git("status", "--porcelain", "-z", cwd=self.source)
        result = self.run_script(ok=False)
        self.assertIn("local sync reverted", result.stderr)
        self.assertIn("push to workstation failed", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.source).strip(), remote_tip)
        self.assertEqual(self.git("status", "--porcelain", "-z", cwd=self.source), status)
        self.assertEqual((self.source / "tracked").read_text(), "remote unstaged edits\n")
        self.assertEqual(self.git("show", ":tracked", cwd=self.source), "remote edits\n")
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn("refs/fetch-sign/", self.git("for-each-ref", "refs/fetch-sign"))

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
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn("refs/fetch-sign/", self.git("for-each-ref", "refs/fetch-sign"))

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
        self.assertIn("Merge conflict in tracked", result.stderr)
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

    def test_mirrors_remote_home_path_for_missing_worktree(self):
        linked = self.mock_ssh_remote()
        self.run_script("topic")
        self.assertTrue(linked.is_dir())
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD", cwd=linked).strip(), "topic")
        self.assertEqual((linked / "file").read_text(), "remote content\n")
        self.assert_signed_history()

    def test_mirrored_worktree_creates_missing_branch(self):
        linked = self.mock_ssh_remote()
        self.git("branch", "-D", "topic")
        self.run_script("topic")
        self.git("verify-commit", "HEAD", cwd=linked)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=linked),
                         self.git("rev-parse", "topic", cwd=self.source))

    def test_mirrored_dry_run_does_not_create_worktree(self):
        linked = self.mock_ssh_remote()
        result = self.run_script("topic", "--dry-run")
        self.assertIn(f"Will create worktree: {linked}", result.stdout)
        self.assertFalse(linked.exists())
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)

    def test_mirrored_existing_destination_refused(self):
        linked = self.mock_ssh_remote()
        linked.mkdir(parents=True)
        (linked / "keep").write_text("preserve")
        result = self.run_script("topic", ok=False)
        self.assertIn("destination already exists", result.stderr)
        self.assertEqual((linked / "keep").read_text(), "preserve")
        self.assertEqual(self.git("for-each-ref", "refs/fetch-sign"), "")

    def test_explicit_worktree_skips_remote_lookup(self):
        mirrored = self.mock_ssh_remote(fail=True)
        linked = self.root / "explicit"
        self.run_script("topic", "--worktree", str(linked))
        self.assertTrue(linked.is_dir())
        self.assertFalse(self.ssh_log.exists())
        self.assertFalse(mirrored.exists())

    def test_existing_checkout_skips_remote_lookup(self):
        self.mock_ssh_remote(fail=True)
        self.git("switch", "topic")
        self.run_script()
        self.assertFalse(self.ssh_log.exists())
        self.assert_signed_history()

    def test_remote_worktree_lookup_failure_preserves_branch(self):
        self.mock_ssh_remote(fail=True)
        result = self.run_script("topic", ok=False)
        self.assertIn("Remote worktree lookup failed", result.stderr)
        self.assertIn("--worktree PATH", result.stderr)
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)
        self.assertEqual(self.git("for-each-ref", "refs/fetch-sign"), "")

    def test_no_matching_remote_worktree_requires_path(self):
        self.mock_ssh_remote(branch="different-topic")
        result = self.run_script("topic", ok=False)
        self.assertIn("No worktree for topic on workstation", result.stderr)
        self.assertIn("--worktree PATH", result.stderr)

    def test_remote_worktree_outside_home_requires_path(self):
        self.mock_ssh_remote(path="/srv/repository-pr/topic")
        result = self.run_script("topic", ok=False)
        self.assertIn("outside its home directory", result.stderr)

    def test_remote_worktree_parent_traversal_refused(self):
        self.mock_ssh_remote(path="/home/remote/../outside/topic")
        result = self.run_script("topic", ok=False)
        self.assertIn("contains '..'", result.stderr)

    def test_ssh_url_port_and_quoted_path(self):
        linked = self.mock_ssh_remote(url="ssh://user@remote:2222/~/repository%20with%20spaces")
        self.run_script("topic", "--dry-run")
        arguments = json.loads(self.ssh_log.read_text())
        self.assertEqual(arguments[:4], ["-p", "2222", "--", "user@remote"])
        self.assertIn('git -C "$HOME"/\'repository with spaces\'', arguments[-1])
        self.assertFalse(linked.exists())

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

    def test_default_skips_dirty_target_without_fetching_or_stashing(self):
        self.git("switch", "topic")
        (self.local / "tracked").write_text("staged\n")
        self.git("add", "tracked")
        (self.local / "tracked").write_text("unstaged\n")
        (self.local / "notes").write_text("untracked\n")
        status = self.git("status", "--porcelain", "-z")
        result = self.run_script(defaults=True)
        self.assertIn("warning: skipping topic", result.stderr)
        self.assertIn("--allow-dirty", result.stderr)
        self.assertEqual(self.git("status", "--porcelain", "-z"), status)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.previous)
        self.assertEqual(self.git("for-each-ref", "refs/fetch-sign"), "")
        self.assertEqual(self.git("stash", "list"), "")

    def test_default_skips_unpushed_commits_despite_stale_tracking_ref(self):
        self.git("switch", "topic")
        self.git("update-ref", "refs/remotes/workstation/topic", self.previous)
        result = self.run_script(defaults=True)
        self.assertIn("warning: skipping topic", result.stderr)
        self.assertIn("--allow-unpushed", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.previous)
        self.assertEqual(self.git("rev-parse", "topic", cwd=self.source).strip(), self.source_before)
        self.assertEqual(self.git("for-each-ref", "refs/fetch-sign"), "")

    def test_default_accepts_clean_local_tip_reachable_from_fresh_source(self):
        self.git("switch", "topic")
        self.git("reset", "--hard", self.base)
        self.previous = self.base
        self.run_script(defaults=True)
        self.assert_signed_history()

    def synced_tip(self):
        return self.git("for-each-ref", "--format=%(objectname)",
                        "refs/fetch-sign/synced").strip()

    def rewrite_remote_tip(self):
        self.git("commit", "--amend", "--allow-empty",
                 "-m", "updated remote commit", cwd=self.source)

    def test_remote_rewrite_accepts_unchanged_last_synced_tip(self):
        self.git("switch", "topic")
        self.run_script()
        previous = self.git("rev-parse", "topic").strip()
        self.assertEqual(self.synced_tip(), previous)
        self.rewrite_remote_tip()
        result = self.run_script(defaults=True)
        self.assertNotIn("warning: skipping", result.stderr)
        self.assertNotEqual(self.git("rev-parse", "topic").strip(), previous)
        self.assertEqual(self.synced_tip(), self.git("rev-parse", "topic").strip())
        self.assertEqual(self.git("rev-parse", "topic").strip(),
                         self.git("rev-parse", "topic", cwd=self.source).strip())
        self.git("verify-commit", "topic")

    def test_remote_rewrite_still_protects_local_commit_after_sync(self):
        self.git("switch", "topic")
        self.run_script()
        synced = self.synced_tip()
        self.git("commit", "--allow-empty", "-m", "new local work")
        local = self.git("rev-parse", "topic").strip()
        self.rewrite_remote_tip()
        result = self.run_script(defaults=True)
        self.assertIn("--allow-unpushed", result.stderr)
        self.assertEqual(self.git("rev-parse", "topic").strip(), local)
        self.assertEqual(self.synced_tip(), synced)

    def test_no_push_does_not_acknowledge_generated_signed_tip(self):
        self.git("switch", "topic")
        self.run_script("--no-push")
        self.assertEqual(self.synced_tip(), "")
        local = self.git("rev-parse", "topic").strip()
        self.rewrite_remote_tip()
        result = self.run_script(defaults=True)
        self.assertIn("--allow-unpushed", result.stderr)
        self.assertEqual(self.git("rev-parse", "topic").strip(), local)

    def test_failed_push_retains_previous_sync_acknowledgement(self):
        self.git("switch", "topic")
        self.run_script()
        synced = self.synced_tip()
        self.rewrite_remote_tip()
        (self.source / "tracked").write_text("remote edits\n")
        result = self.run_script(defaults=True, ok=False)
        self.assertIn("local sync reverted", result.stderr)
        self.assertEqual(self.synced_tip(), synced)
        self.assertEqual(self.git("rev-parse", "topic").strip(), synced)
        self.git("restore", "tracked", cwd=self.source)
        self.run_script(defaults=True)
        self.assertEqual(self.synced_tip(), self.git("rev-parse", "topic").strip())

    def test_sync_acknowledgement_is_scoped_to_source_remote(self):
        self.git("switch", "topic")
        self.run_script()
        local = self.git("rev-parse", "topic").strip()
        self.git("remote", "add", "other-source", str(self.source))
        self.rewrite_remote_tip()
        result = self.run_script(defaults=True, remote="other-source")
        self.assertIn("--allow-unpushed", result.stderr)
        self.assertEqual(self.git("rev-parse", "topic").strip(), local)

    def test_dry_run_does_not_acknowledge_sync(self):
        self.git("switch", "topic")
        self.git("reset", "--hard", self.base)
        self.run_script("--dry-run", defaults=True)
        self.assertEqual(self.synced_tip(), "")

    def test_dirty_override_does_not_allow_unpushed_commits(self):
        self.git("switch", "topic")
        (self.local / "notes").write_text("keep\n")
        result = self.run_script("--allow-dirty", defaults=True)
        self.assertIn("--allow-unpushed", result.stderr)
        self.assertEqual((self.local / "notes").read_text(), "keep\n")
        self.assertEqual(self.git("stash", "list"), "")

    def test_unpushed_override_does_not_allow_dirty_worktrees(self):
        self.git("switch", "topic")
        (self.local / "notes").write_text("keep\n")
        result = self.run_script("--allow-unpushed", defaults=True)
        self.assertIn("--allow-dirty", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.previous)

    def test_failed_push_restores_dirty_index_and_original_stash(self):
        self.git("switch", "topic")
        (self.local / "other").write_text("older stash\n")
        self.git("stash", "push", "-m", "older")
        older = self.git("rev-parse", "refs/stash")
        (self.local / "tracked").write_text("staged\n")
        self.git("add", "tracked")
        (self.local / "tracked").write_text("unstaged\n")
        (self.local / "notes").write_text("notes\n")
        (self.local / "link").symlink_to("notes")
        status = self.git("status", "--porcelain", "-z")
        index = self.git("diff", "--cached", "--binary")
        edits = self.git("diff", "--binary")
        (self.source / "tracked").write_text("remote edit\n")
        result = self.run_script(ok=False)
        self.assertIn("local sync reverted", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.previous)
        self.assertEqual(self.git("status", "--porcelain", "-z"), status)
        self.assertEqual(self.git("diff", "--cached", "--binary"), index)
        self.assertEqual(self.git("diff", "--binary"), edits)
        self.assertEqual((self.local / "notes").read_text(), "notes\n")
        self.assertEqual(os.readlink(self.local / "link"), "notes")
        self.assertEqual(self.git("rev-parse", "refs/stash"), older)

    def test_failed_push_removes_only_new_worktree_and_restores_existing_branch(self):
        path = self.root / "new-worktree"
        (self.source / "tracked").write_text("remote edit\n")
        result = self.run_script("topic", "--worktree", str(path), ok=False)
        self.assertIn("local sync reverted", result.stderr)
        self.assertFalse(path.exists())
        self.assertEqual(self.git("rev-parse", "topic").strip(), self.previous)

    def test_failed_push_removes_only_new_worktree_and_new_branch(self):
        self.git("branch", "-D", "topic")
        path = self.root / "new-worktree"
        (self.source / "tracked").write_text("remote edit\n")
        result = self.run_script("topic", "--worktree", str(path), ok=False)
        self.assertIn("local sync reverted", result.stderr)
        self.assertFalse(path.exists())
        self.assertEqual(self.git("for-each-ref", "refs/heads/topic"), "")

    def test_push_hook_concurrent_edit_prevents_destructive_rollback(self):
        self.git("switch", "topic")
        hook = self.local / ".git" / "hooks" / "pre-push"
        hook.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
                        "Path('file').write_text('concurrent edit\\n')\nraise SystemExit(1)\n")
        hook.chmod(0o755)
        result = self.run_script(ok=False)
        self.assertIn("recovery failed", result.stderr)
        self.assertEqual((self.local / "file").read_text(), "concurrent edit\n")
        self.assertIn(self.previous, self.git("for-each-ref", "refs/fetch-sign"))

    def test_push_hook_concurrent_dirty_edit_retains_original_stash(self):
        self.git("switch", "topic")
        (self.local / "notes").write_text("before\n")
        hook = self.local / ".git" / "hooks" / "pre-push"
        hook.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
                        "Path('notes').write_text('concurrent edit\\n')\nraise SystemExit(1)\n")
        hook.chmod(0o755)
        result = self.run_script(ok=False)
        self.assertIn("Worktree changed during push", result.stderr)
        self.assertEqual((self.local / "notes").read_text(), "concurrent edit\n")
        stash = self.git("rev-parse", "refs/stash").strip()
        self.assertEqual(self.git("show", f"{stash}^3:notes"), "before\n")



if __name__ == "__main__":
    unittest.main()
