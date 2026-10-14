"""Guix attestation integration tests: real Git/GPG, simulated SCP download."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "guix.sh"


@unittest.skipUnless(shutil.which("gpg") and shutil.which("git"),
                     "Git and GnuPG are required")
class GuixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix="guix-test-key-")
        cls.addClassCleanup(cls.keys.cleanup)
        os.chmod(cls.keys.name, 0o700)
        cls.env = dict(os.environ)
        for name in ("SIGNER", "SIGS_DIR", "REMOTE_SIGS_DIR", "BASE_BRANCH",
                     "GPG_KEY", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            cls.env.pop(name, None)
        cls.env.update(GNUPGHOME=cls.keys.name, GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        cls.gpg = shutil.which("gpg")
        result = subprocess.run(
            [cls.gpg, "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
             "--quick-generate-key", "Guix Test <guix@example.com>",
             "ed25519", "sign", "0"], env=cls.env, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr.decode())
        listing = subprocess.check_output(
            [cls.gpg, "--with-colons", "--list-secret-keys"], env=cls.env,
            stderr=subprocess.DEVNULL).decode()
        cls.fingerprint = next(line.split(":")[9] for line in listing.splitlines()
                               if line.startswith("fpr:"))

    @classmethod
    def tearDownClass(cls):
        if shutil.which("gpgconf"):
            subprocess.run(["gpgconf", "--kill", "gpg-agent"],
                           env=cls.env, check=False, capture_output=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="guix-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(type(self).env)
        self.upstream = self.root / "upstream"
        self.repo = self.root / "local signatures"
        self.download = self.root / "remote signer"
        self.download.mkdir()
        self.checksum = self.download / "noncodesigned.SHA256SUMS"
        self.checksum.write_text("abc123  release.tar.gz\n")
        (self.download / "noncodesigned.SHA256SUMS.asc").write_text("stale remote signature\n")
        (self.download / "private-build-notes").write_text("not an attestation\n")
        self.git("init", "-b", "main", str(self.upstream), cwd=self.root)
        self.configure(self.upstream)
        (self.upstream / "README").write_text("Signatures\n")
        self.git("add", "README", cwd=self.upstream)
        self.git("commit", "-m", "initial", cwd=self.upstream)
        self.git("clone", str(self.upstream), str(self.repo), cwd=self.root)
        self.configure(self.repo)
        self.initial = self.git("rev-parse", "HEAD").strip()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.scp_log = self.root / "scp.json"
        self.gpg_log = self.root / "gpg.jsonl"
        self.mock("scp", """
import shutil
Path(os.environ['SCP_LOG']).write_text(json.dumps(sys.argv[1:]))
shutil.copytree(os.environ['TEST_DOWNLOAD'], sys.argv[-1], symlinks=True)
""")
        self.mock("gpg", """
import subprocess
with open(os.environ['GPG_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
if os.environ.get('TEST_SIGN_FAIL'):
    sys.exit(1)
sys.exit(subprocess.run([os.environ['TEST_GPG'], *sys.argv[1:]]).returncode)
""")
        self.env.update(
            PATH=str(self.bin) + os.pathsep + self.env["PATH"],
            SIGNER="Alice", SIGS_DIR=str(self.repo), GPG_KEY=self.fingerprint,
            TEST_DOWNLOAD=str(self.download), TEST_GPG=self.gpg,
            SCP_LOG=str(self.scp_log), GPG_LOG=str(self.gpg_log),
            TMPDIR=str(self.root))

    def configure(self, repo):
        for key, value in (("user.name", "Guix Test"),
                           ("user.email", "guix@example.com"),
                           ("commit.gpgsign", "false")):
            self.git("config", key, value, cwd=repo)

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd or self.repo,
                                       env=self.env, stderr=subprocess.PIPE).decode()

    def mock(self, name, body):
        script = self.bin / name
        script.write_text(f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n" + body)
        script.chmod(0o755)

    def run_script(self, *extra, project="bitcoin", version="31.0", ok=True):
        result = subprocess.run(
            [str(SCRIPT), project, version, "build-host", *extra],
            env=self.env, cwd=self.root, text=True, capture_output=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(self.root.glob("guix-attest.*")), [])
        return result

    def assert_unchanged(self):
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.initial)
        self.assertEqual(self.git("branch", "--show-current").strip(), "main")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def assert_signatures(self, codesigned=False):
        sums = ["noncodesigned.SHA256SUMS"]
        if codesigned:
            sums.append("all.SHA256SUMS")
        expected = []
        for checksum in sums:
            path = self.repo / "31.0/Alice" / checksum
            expected.extend([str(path.relative_to(self.repo)),
                             str(path.relative_to(self.repo)) + ".asc"])
            self.assertEqual(path.read_bytes(), (self.download / checksum).read_bytes())
            subprocess.run([self.gpg, "--verify", str(path) + ".asc", str(path)],
                           env=self.env, check=True, capture_output=True)
        changed = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
        self.assertEqual(sorted(changed.splitlines()), sorted(expected))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_unsigned_local_commit_excludes_remote_extras(self):
        self.run_script()
        self.assertEqual(self.git("branch", "--show-current").strip(), "31.0-Alice")
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(),
                         "Add Alice attestations for v31.0 unsigned")
        self.assert_signatures()
        refs = self.git("ls-remote", "--heads", "origin")
        self.assertNotIn("31.0-Alice", refs)

    def test_codesigned_explicit_push_to_fork(self):
        (self.download / "all.SHA256SUMS").write_text("def456  signed.zip\n")
        fork = self.root / "fork.git"
        self.git("init", "--bare", str(fork), cwd=self.root)
        self.git("remote", "add", "my-fork", str(fork))
        self.run_script("--push", "my-fork")
        self.assertEqual(self.git("branch", "--show-current").strip(),
                         "31.0-Alice-codesigned")
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "@{upstream}").strip(),
                         "my-fork/31.0-Alice-codesigned")
        self.assert_signatures(codesigned=True)
        self.assertNotIn("31.0-Alice", self.git("ls-remote", "--heads", "origin"))

    def test_sv2_project_custom_paths_base_and_key(self):
        self.git("branch", "release")
        self.git("branch", "--set-upstream-to=origin/main", "release")
        self.env.update(REMOTE_SIGS_DIR="/srv/build/sigs", BASE_BRANCH="release")
        self.run_script(project="sv2-tp")
        args = json.loads(self.scp_log.read_text())
        self.assertEqual(args[:2], ["-r", "build-host:/srv/build/sigs/31.0/Alice"])
        args = json.loads(self.gpg_log.read_text().splitlines()[0])
        self.assertEqual(args[args.index("--local-user") + 1], self.fingerprint)
        self.assert_signatures()

    def test_sv2_default_remote_path_and_default_gpg_key(self):
        self.env.pop("GPG_KEY")
        self.run_script(project="sv2-tp")
        self.assertEqual(json.loads(self.scp_log.read_text())[1],
                         "build-host:sv2-tp-guix.sigs/31.0/Alice")
        self.assert_signatures()

    def test_missing_signer_and_unsafe_paths_rejected_before_download(self):
        for name, value in (("SIGNER", ""), ("SIGNER", "../Alice"),
                            ("REMOTE_SIGS_DIR", "sigs; touch unwanted")):
            with self.subTest(name=name, value=value):
                env = dict(self.env)
                self.env[name] = value
                self.run_script(ok=False)
                self.env = env
                self.assertFalse(self.scp_log.exists())
                self.assert_unchanged()
        self.run_script(version="../31.0", ok=False)
        self.assertFalse(self.scp_log.exists())

    def test_staged_edits_preserved(self):
        (self.repo / "README").write_text("local edit\n")
        self.git("add", "README")
        before = self.git("diff", "--cached")
        self.run_script(ok=False)
        self.assertEqual(self.git("diff", "--cached"), before)
        self.assertFalse(self.scp_log.exists())

    def test_untracked_files_preserved(self):
        path = self.repo / "private-notes"
        path.write_text("local notes\n")
        self.run_script(ok=False)
        self.assertEqual(path.read_text(), "local notes\n")
        self.assertFalse(self.scp_log.exists())

    def test_missing_and_symlinked_checksums_rejected(self):
        self.checksum.unlink()
        self.run_script(ok=False)
        self.assert_unchanged()
        target = self.root / "other-checksum"
        target.write_text("checksum\n")
        self.checksum.symlink_to(target)
        self.run_script(ok=False)
        self.assert_unchanged()

    def test_signing_failure_leaves_repo_unchanged(self):
        self.env["TEST_SIGN_FAIL"] = "1"
        self.run_script(ok=False)
        self.assert_unchanged()

    def test_existing_branch_refused_before_signing(self):
        self.git("branch", "31.0-Alice")
        self.run_script(ok=False)
        self.assertFalse(self.gpg_log.exists())
        self.assert_unchanged()

    def test_unknown_push_remote_refused_before_download(self):
        self.run_script("--push", "missing", ok=False)
        self.assertFalse(self.scp_log.exists())
        self.assert_unchanged()

    def test_divergent_base_refuses_merge(self):
        self.git("commit", "--allow-empty", "-m", "local commit")
        local_tip = self.git("rev-parse", "HEAD").strip()
        self.git("commit", "--allow-empty", "-m", "upstream commit", cwd=self.upstream)
        self.run_script(ok=False)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), local_tip)
        self.assertEqual(self.git("branch", "--show-current").strip(), "main")
        self.assertNotIn("31.0-Alice", self.git("branch", "--list"))


if __name__ == "__main__":
    unittest.main()
