"""Exercise the signing workflow with fake tools; no Apple credentials/network."""

import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "sign-apples.sh"
SUBMISSION = "12345678-1234-1234-1234-123456789abc"

# Model the relevant signapple contract: detached paths include bundle names,
# apply maps a signature directory onto a binary directory, and universal
# slices are applied together. Failure injection also models apply's zero-exit
# failure reporting and notarize returning before an independent status check.
MOCK_TOOL = r'''
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
tool = Path(sys.argv[0]).name
with open(os.environ["TOOL_LOG"], "a") as log:
    log.write(json.dumps([tool, *args]) + "\n")
command = args[0]
if os.environ.get("WATCH_OUTPUT"):
    output = Path(os.environ["WATCH_OUTPUT"])
    for relative in json.loads(os.environ["WATCH_FILES"]):
        assert not (output / relative).exists(), "Published before all targets succeeded"
if os.environ.get("FAIL_COMMAND") == command:
    sys.exit(1)
if command in ("--help", "--find"):
    sys.exit(0)

if tool == "scp":
    import shutil
    assert len(args) == 2, "Use one remote source and one SSH connection"
    assert Path(args[1]).is_dir()
    sources = sorted(Path(os.environ["REMOTE_ARCHIVES"]).glob(Path(args[0]).name))
    assert sources, "Remote glob matched no archives"
    for source in sources:
        if os.environ.get("FAIL_FETCH") in ("all", source.name):
            sys.exit(1)
        shutil.copyfile(source, Path(args[1]) / source.name)
elif tool == "xcrun":
    assert args[:2] == ["notarytool", "info"]
    print(os.environ.get("NOTARY_INFO", '{"status": "Accepted"}'))
elif command == "sign":
    assert args[1] == "--passphrase" and args[3] == "--hardened-runtime"
    target = Path(args[5])
    if os.environ.get("REMOTE_ARCHIVES"):
        assert target.stat().st_mode & 0o111, "Executable mode lost during extraction"
    stage = Path(args[args.index("--detach") + 1])
    if target.is_dir():
        dest = stage / target.name / "Contents/MacOS/main.arm64sign"
        dest.parent.mkdir(parents=True)
        dest.write_text("signature")
        resources = stage / target.name / "Contents/_CodeSignature/CodeResources"
        resources.parent.mkdir()
        resources.write_text("resources")
    else:
        default_arch = "x86_64" if any(p.startswith("x86_64") for p in target.parts) else "arm64"
        for arch in os.environ.get("SIGN_ARCHES", default_arch).split(","):
            (stage / (target.name + "." + arch + "sign")).write_text("signature")
elif command == "apply":
    assert args[1] == "--no-verify"
    if os.environ.get("APPLY_SILENT_FAIL"):
        print("Failed to apply code signature")
        sys.exit(0)
    root, stage = map(Path, args[2:])
    assert list(stage.rglob("*sign"))
    for signature in stage.rglob("*sign"):
        binary = root / signature.relative_to(stage).with_suffix("")
        binary.write_text("signed")
    for resource in stage.rglob("CodeResources"):
        dest = root / resource.relative_to(stage)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resource.read_bytes())
elif command == "verify":
    target = Path(args[1])
    binary = target / "Contents/MacOS/main" if target.is_dir() else target
    assert binary.read_text() == "signed"
elif command == "notarize":
    assert args[1] == "--passphrase"
    target = Path(args[-1])
    if target.name == os.environ.get("FAIL_TARGET"):
        sys.exit(1)
    if not os.environ.get("NO_SUBMISSION"):
        print("Notarization ID: " + os.environ.get("SUBMISSION", "12345678-1234-1234-1234-123456789abc"))
    if target.is_dir():
        stage = Path(args[args.index("--detach") + 1])
        (stage / target.name / "Contents/CodeResources").write_text("ticket")
else:
    raise AssertionError(args)
'''


class SignApplesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sign-apples-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.inputs = self.root / "input files"
        self.inputs.mkdir()
        self.output = self.root / "detached signatures"
        self.certificate = self.root / "signing certificate.p12"
        self.certificate.touch()
        self.api_key = self.root / "AuthKey_TESTKEY.p8"
        self.api_key.touch()
        self.log = self.root / "tools.jsonl"
        mock_bin = self.root / "mock bin"
        mock_bin.mkdir()
        for name in ("signapple", "xcrun", "scp"):
            tool = mock_bin / name
            tool.write_text(f"#!{sys.executable}\n" + MOCK_TOOL)
            tool.chmod(0o755)
        self.env = dict(os.environ)
        for name in ("SIGNAPPLE_PASSPHRASE", "SIGNAPPLE_NOTARY_PASSPHRASE",
                     "SIGNAPPLE_CERTIFICATE", "SIGNAPPLE_API_KEY",
                     "SIGNAPPLE_KEY_ID", "SIGNAPPLE_ISSUER", "SV2_TP_DETACHED_SIGS_DIR",
                     "FAIL_COMMAND", "NOTARY_INFO", "APPLY_SILENT_FAIL",
                     "NO_SUBMISSION", "SUBMISSION", "SIGN_ARCHES", "FAIL_TARGET",
                     "WATCH_OUTPUT", "WATCH_FILES", "REMOTE_ARCHIVES", "FAIL_FETCH"):
            self.env.pop(name, None)
        self.env.update(PATH=str(mock_bin) + os.pathsep + self.env["PATH"],
                        TOOL_LOG=str(self.log), TMPDIR=str(self.root), SIGNAPPLE_PASSPHRASE="fake signing secret")
        self.targets = ["arm64/pkg/bin/program", "x86_64/pkg/bin/program"]
        for target in self.targets:
            binary = self.inputs / target
            binary.parent.mkdir(parents=True)
            binary.write_text("unsigned")

    def arguments(self, targets=None):
        return ["--certificate", str(self.certificate.relative_to(self.root)),
                "--api-key", str(self.api_key.relative_to(self.root)),
                "--key-id", "TESTKEY", "--issuer", "test-issuer",
                "--input-dir", str(self.inputs.relative_to(self.root)),
                "--output-dir", str(self.output.relative_to(self.root)),
                "--", *(self.targets if targets is None else targets)]

    def run_script(self, args=None, ok=True, trace=False, output_unchanged=True):
        original_output = self.output_state()
        command = ["/bin/bash", "-x", str(SCRIPT)] if trace else [str(SCRIPT)]
        result = subprocess.run(
            [*command, *(self.arguments() if args is None else args)],
            cwd=self.root, env=self.env, input="", capture_output=True, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            if output_unchanged:
                self.assertEqual(self.output_state(), original_output)
        self.assertEqual(list(self.root.rglob(".sign-apples.*")), [])
        return result

    def output_state(self):
        if self.output.is_symlink():
            return {".": ("symlink", os.readlink(self.output))}
        if not self.output.exists():
            return {}
        result = {}
        for path in [self.output, *self.output.rglob("*")]:
            name = str(path.relative_to(self.output))
            if path.is_symlink():
                result[name] = ("symlink", os.readlink(path))
            elif path.is_dir():
                result[name] = ("directory", path.stat().st_mode)
            else:
                result[name] = ("file", path.read_bytes(), path.stat().st_mode)
        return result

    def calls(self, command=None):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [call for call in calls if command is None or call[1] == command]

    def write_env(self, path=None, extra=""):
        path = path or self.root / ".env"
        path.write_text(
            f'SIGNAPPLE_CERTIFICATE="{self.certificate}"\n'
            f'SIGNAPPLE_API_KEY="{self.api_key}"\n'
            'SIGNAPPLE_KEY_ID=TESTKEY\n'
            'export SIGNAPPLE_ISSUER=test-issuer # comment\n' + extra)
        return path

    def configured_arguments(self, targets=None):
        # Keep just input/output options and targets; credentials come from config.
        return self.arguments(targets)[8:]

    def test_default_env_file_supplies_credentials_and_passphrases(self):
        self.env.pop("SIGNAPPLE_PASSPHRASE")
        self.write_env(extra="SIGNAPPLE_PASSPHRASE='file secret # with = symbols'\n"
                             "SIGNAPPLE_NOTARY_PASSPHRASE=''\n")
        self.run_script(self.configured_arguments())
        self.assertEqual(self.calls("sign")[0][3], "file secret # with = symbols")
        self.assertEqual(self.calls("notarize")[0][3], "")
        self.assertEqual(self.calls("notarytool")[0][9], "test-issuer")

    def test_explicit_env_file_resolves_its_own_relative_paths(self):
        config_dir = self.root / "config files"
        config_dir.mkdir()
        config = config_dir / "signing.env"
        config.write_text(
            'SIGNAPPLE_CERTIFICATE="../signing certificate.p12"\n'
            'SIGNAPPLE_API_KEY=../AuthKey_TESTKEY.p8\n'
            'SIGNAPPLE_KEY_ID=TESTKEY\nSIGNAPPLE_ISSUER=test-issuer\n')
        # The default file must not be read when a different file is selected.
        (self.root / ".env").write_text("invalid default file\n")
        self.run_script(["--env-file", str(config), *self.configured_arguments()])
        self.assertEqual(Path(self.calls("sign")[0][5]).resolve(), self.certificate)

    def test_config_credential_paths_expand_home(self):
        self.env["HOME"] = str(self.root)
        config = self.write_env()
        config.write_text(config.read_text().replace(str(self.root), "~"))
        self.run_script(self.configured_arguments())
        self.assertEqual(self.calls("sign")[0][5], str(self.certificate))

    def test_cli_overrides_environment_which_overrides_file(self):
        self.write_env(extra="SIGNAPPLE_PASSPHRASE='file secret'\n"
                             "SIGNAPPLE_NOTARY_PASSPHRASE='file API secret'\n")
        self.env.update(SIGNAPPLE_CERTIFICATE="missing-environment-certificate",
                        SIGNAPPLE_API_KEY=str(self.api_key), SIGNAPPLE_KEY_ID="TESTKEY",
                        SIGNAPPLE_ISSUER="environment-issuer", SIGNAPPLE_NOTARY_PASSPHRASE="")
        self.run_script(["--certificate", str(self.certificate), "--issuer", "cli-issuer",
                         *self.configured_arguments()])
        self.assertEqual(self.calls("sign")[0][3], "fake signing secret")
        self.assertEqual(self.calls("notarize")[0][3], "")
        self.assertEqual(self.calls("notarytool")[0][9], "cli-issuer")

    def test_empty_environment_credential_overrides_file(self):
        self.write_env()
        self.env["SIGNAPPLE_CERTIFICATE"] = ""
        self.run_script(self.configured_arguments(), ok=False)
        self.assertEqual(self.calls(), [])

    def test_file_values_do_not_execute_shell_commands(self):
        self.env.pop("SIGNAPPLE_PASSPHRASE")
        self.write_env(extra="SIGNAPPLE_PASSPHRASE='$(touch unexpected-file)'\n")
        result = self.run_script(self.configured_arguments(), trace=True)
        self.assertFalse((self.root / "unexpected-file").exists())
        self.assertEqual(self.calls("sign")[0][3], "$(touch unexpected-file)")
        self.assertNotIn("$(touch unexpected-file)", result.stdout + result.stderr)

    def test_malformed_or_unsupported_config_fails_before_signing(self):
        for contents in ("SIGNAPPLE_KEY_ID='unclosed\n", "PATH=/somewhere\n",
                         "SIGNAPPLE_INPUT_DIR=/somewhere\n", "echo unexpected\n",
                         "SIGNAPPLE_ISSUER=unquoted spaces\n",
                         "SIGNAPPLE_KEY_ID=FIRST\nSIGNAPPLE_KEY_ID=SECOND\n"):
            with self.subTest(contents=contents):
                (self.root / ".env").write_text(contents)
                self.run_script(ok=False)
                self.assertEqual(self.calls(), [])

    def test_missing_explicit_env_file_fails(self):
        result = self.run_script(["--env-file", "missing.env", *self.arguments()], ok=False)
        self.assertIn("Unreadable environment file", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_empty_env_file_works_with_explicit_credentials(self):
        (self.root / ".env").touch()
        self.run_script()

    def test_sv2_tp_project_selects_all_four_targets(self):
        self.write_env()
        targets = [f"{host}/sv2-tp-1.0.3/bin/{binary}"
                   for host in ("arm64-apple-darwin", "x86_64-apple-darwin")
                   for binary in ("sv2-tp", "test_sv2")]
        for target in targets:
            binary = self.inputs / target
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("unsigned")
        args = self.configured_arguments([])[:-1]  # Drop the '--' separator.
        self.run_script([*args, "--project", "sv2-tp", "--release", "1.0.3"])
        self.assertEqual([call[6] for call in self.calls("sign")],
                         [str(self.inputs / target) for target in targets])
        self.assertEqual(sorted(str(path.relative_to(self.output)) for path in self.output.rglob("*sign")),
                         sorted(target + (".arm64sign" if target.startswith("arm64") else ".x86_64sign")
                                for target in targets))
        self.assertEqual(len(self.calls("notarytool")), 4)

    def remote_arguments(self, repo=None, include_output=True):
        args = self.arguments([])[:-1]
        index = args.index("--input-dir")
        args[index:index + 2] = ["--remote", "builder@guix"]
        args += ["--project", "sv2-tp", "--release", "1.0.3"]
        if not include_output:
            index = args.index("--output-dir")
            del args[index:index + 2]
        if repo is not None:
            args += ["--remote-repo", repo]
        return args

    def test_project_output_defaults_to_configured_repository_osx_directory(self):
        self.env["HOME"] = str(self.root)
        self.write_env(extra="SV2_TP_DETACHED_SIGS_DIR='~/sig repo/'\n")
        self.output = self.root / "sig repo/osx"
        self.remote_archives()
        result = self.run_script(self.remote_arguments(include_output=False))
        self.assertIn(f"Signatures written to {self.output}", result.stdout)
        self.assertEqual(len(list(self.output.rglob("*sign"))), 4)

    def test_project_repository_path_is_relative_to_explicit_env_file(self):
        config = self.root / "config files/signing.env"
        config.parent.mkdir()
        self.write_env(config, extra="SV2_TP_DETACHED_SIGS_DIR='sig repo'\n")
        self.output = config.parent / "sig repo/osx"
        self.remote_archives()
        self.run_script(["--env-file", str(config),
                         *self.remote_arguments(include_output=False)])
        self.assertEqual(len(list(self.output.rglob("*sign"))), 4)

    def test_environment_repository_overrides_file(self):
        self.write_env(extra="SV2_TP_DETACHED_SIGS_DIR=unused-repo\n")
        self.env["SV2_TP_DETACHED_SIGS_DIR"] = str(self.root / "environment repo")
        self.output = self.root / "environment repo/osx"
        self.remote_archives()
        self.run_script(self.remote_arguments(include_output=False))
        self.assertEqual(len(list(self.output.rglob("*sign"))), 4)
        self.assertFalse((self.root / "unused-repo").exists())

    def test_explicit_output_overrides_project_repository(self):
        self.write_env(extra="SV2_TP_DETACHED_SIGS_DIR=unused-repo\n")
        self.remote_archives()
        self.run_script(self.remote_arguments())
        self.assertEqual(len(list(self.output.rglob("*sign"))), 4)
        self.assertFalse((self.root / "unused-repo").exists())

    def test_missing_or_empty_repository_requires_explicit_project_output(self):
        self.write_env(extra="SV2_TP_DETACHED_SIGS_DIR=unused-repo\n")
        self.env["SV2_TP_DETACHED_SIGS_DIR"] = ""
        self.run_script(self.remote_arguments(include_output=False), ok=False)
        self.assertEqual(self.calls(), [])
        self.env.pop("SV2_TP_DETACHED_SIGS_DIR")
        self.write_env()
        self.run_script(self.remote_arguments(include_output=False), ok=False)
        self.assertEqual(self.calls(), [])

    def test_project_repository_does_not_default_output_for_explicit_targets(self):
        self.write_env(extra="SV2_TP_DETACHED_SIGS_DIR=unused-repo\n")
        args = self.arguments()
        index = args.index("--output-dir")
        del args[index:index + 2]
        self.run_script(args, ok=False)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.root / "unused-repo").exists())

    def remote_archives(self, bad_entry=None, missing_binary=False):
        # Match contrib/guix/libexec/package.sh: find . archives the extraction
        # root, a helper script, and the versioned installation directory.
        archives = self.root / "remote archives"
        archives.mkdir(exist_ok=True)
        self.env["REMOTE_ARCHIVES"] = str(archives)
        self.env["HELPER_MARKER"] = str(self.root / "helper-executed")
        for host in ("arm64-apple-darwin", "x86_64-apple-darwin"):
            archive = archives / f"sv2-tp-1.0.3-{host}-codesigning.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                for directory in (".", "./sv2-tp-1.0.3", "./sv2-tp-1.0.3/bin"):
                    member = tarfile.TarInfo(directory)
                    member.type = tarfile.DIRTYPE
                    output.addfile(member)
                helper = b'#!/bin/sh\nprintf executed > "$HELPER_MARKER"\nexit 99\n'
                member = tarfile.TarInfo("./detached-sig-create.sh")
                member.size = len(helper)
                member.mode = 0o755
                output.addfile(member, io.BytesIO(helper))
                for binary in ("sv2-tp", "test_sv2"):
                    if missing_binary and binary == "test_sv2":
                        continue
                    member = tarfile.TarInfo(f"./sv2-tp-1.0.3/bin/{binary}")
                    member.size = len(b"unsigned")
                    member.mode = 0o755
                    output.addfile(member, io.BytesIO(b"unsigned"))
                if bad_entry:
                    output.addfile(bad_entry)
        return archives

    def test_remote_fetches_both_archives_signs_four_targets_and_cleans_up(self):
        self.remote_archives()
        self.run_script(self.remote_arguments())
        copies = [call for call in self.calls() if call[0] == "scp"]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0][1],
                         "builder@guix:sv2-tp/guix-build-1.0.3/output/*-apple-darwin/"
                         "sv2-tp-1.0.3-*-apple-darwin-codesigning.tar.gz")
        self.assertEqual(len(self.calls("sign")), 4)
        self.assertEqual(len(self.calls("notarytool")), 4)
        self.assertEqual(len(list(self.output.rglob("*sign"))), 4)
        for call in self.calls("sign"):
            self.assertFalse(Path(call[6]).exists(), "Fetched inputs were not cleaned up")
        self.assertFalse(Path(self.env["HELPER_MARKER"]).exists(), "Archive helper was executed")

    def git(self, repo, *args):
        result = subprocess.run(["git", "-C", str(repo), *args], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def signatures_repository(self):
        repo = self.root / "signature repo"
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.name", "Signing Test")
        self.git(repo, "config", "user.email", "test@example.invalid")
        self.git(repo, "config", "commit.gpgsign", "false")
        self.git(repo, "config", "core.hooksPath", str(repo / ".git/hooks"))
        (repo / "README").write_text("existing signatures repository")
        self.git(repo, "add", "README")
        self.git(repo, "commit", "-qm", "Initial commit")
        self.output = repo / "osx"
        return repo

    def test_signatures_are_committed_without_unrelated_index_or_worktree_changes(self):
        repo = self.signatures_repository()
        (repo / "README").write_text("unrelated unstaged edit")
        (repo / "staged-note").write_text("unrelated staged addition")
        (repo / "untracked-note").touch()
        self.git(repo, "add", "staged-note")
        self.remote_archives()
        self.run_script(self.remote_arguments())
        self.assertEqual(self.git(repo, "log", "-1", "--format=%s"),
                         "Add 1.0.3 detached macOS signatures")
        expected = sorted(f"osx/{host}/sv2-tp-1.0.3/bin/{binary}.{arch}sign"
                          for host, arch in (("arm64-apple-darwin", "arm64"),
                                             ("x86_64-apple-darwin", "x86_64"))
                          for binary in ("sv2-tp", "test_sv2"))
        self.assertEqual(self.git(repo, "diff-tree", "--no-commit-id", "--name-only",
                                  "-r", "HEAD").splitlines(), expected)
        self.assertEqual(self.git(repo, "diff", "--cached", "--name-only"), "staged-note")
        self.assertEqual((repo / "README").read_text(), "unrelated unstaged edit")
        self.assertTrue((repo / "untracked-note").exists())
        self.assertEqual(self.git(repo, "diff", "--name-only"), "README")

    def test_commit_paths_are_literal_and_explicit_targets_are_supported(self):
        repo = self.signatures_repository()
        target = ":(glob)*"
        (self.inputs / target).write_text("unsigned")
        self.run_script(self.arguments([target]))
        self.assertEqual(self.git(repo, "log", "-1", "--format=%s"),
                         "Add detached macOS signatures")
        self.assertEqual(self.git(repo, "diff-tree", "--no-commit-id", "--name-only",
                                  "-r", "HEAD"), "osx/:(glob)*.arm64sign")
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")

    def test_signing_failure_does_not_commit(self):
        repo = self.signatures_repository()
        before = self.git(repo, "rev-parse", "HEAD")
        self.remote_archives()
        self.env["FAIL_TARGET"] = "test_sv2"
        self.run_script(self.remote_arguments(), ok=False)
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), before)
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")

    def test_commit_failure_keeps_published_signatures_for_manual_commit(self):
        repo = self.signatures_repository()
        before = self.git(repo, "rev-parse", "HEAD")
        hook = repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.remote_archives()
        result = self.run_script(self.remote_arguments(), ok=False, output_unchanged=False)
        self.assertIn("commit them manually", result.stderr)
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), before)
        self.assertEqual(len(list(self.output.rglob("*sign"))), 4)
        self.assertEqual(len(self.git(repo, "diff", "--cached", "--name-only").splitlines()), 4)

    def test_remote_missing_architecture_archive_stops_before_signing(self):
        archives = self.remote_archives()
        (archives / "sv2-tp-1.0.3-x86_64-apple-darwin-codesigning.tar.gz").unlink()
        self.run_script(self.remote_arguments(), ok=False)
        self.assertEqual(self.calls("sign"), [])
        self.assertEqual(len([call for call in self.calls() if call[0] == "scp"]), 1)

    def test_remote_archive_root_directory_entry_is_allowed(self):
        for name in (".", "./"):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                member.type = tarfile.DIRTYPE
                self.remote_archives(bad_entry=member)
                self.run_script(self.remote_arguments())
                self.assertEqual(len(list(self.output.rglob("*sign"))), 4)
                shutil.rmtree(self.output)

    def test_remote_checkout_override(self):
        self.remote_archives()
        self.run_script(self.remote_arguments("/srv/build/sv2-tp"))
        copies = [call for call in self.calls() if call[0] == "scp"]
        self.assertTrue(all(call[1].startswith("builder@guix:/srv/build/sv2-tp/")
                            for call in copies))

    def test_remote_fetch_failure_never_signs_or_publishes(self):
        self.remote_archives()
        for failure in ("all", "sv2-tp-1.0.3-x86_64-apple-darwin-codesigning.tar.gz"):
            with self.subTest(failure=failure):
                self.env["FAIL_FETCH"] = failure
                self.run_script(self.remote_arguments(), ok=False)
                self.assertEqual(self.calls("sign"), [])

    def test_remote_notarization_failure_cleans_up_inputs(self):
        self.remote_archives()
        self.env["FAIL_TARGET"] = "test_sv2"
        self.run_script(self.remote_arguments(), ok=False)
        self.assertEqual(len(self.calls("sign")), 2)

    def test_remote_missing_targets_are_detected_before_signing(self):
        self.remote_archives(missing_binary=True)
        self.run_script(self.remote_arguments(), ok=False)
        self.assertEqual(self.calls("sign"), [])

    def test_remote_unsafe_archives_are_rejected_before_signing(self):
        entries = [tarfile.TarInfo(name) for name in
                   (".", "./", "../../escaped", "/tmp/escaped",
                    "sv2-tp-1.0.3/../../escaped")]
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            for name in ("sv2-tp-1.0.3/link", "./detached-sig-create.sh"):
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = "../../escaped"
                entries.append(member)
        for member in entries:
            with self.subTest(name=member.name, kind=member.type):
                self.remote_archives(bad_entry=member)
                result = self.run_script(self.remote_arguments(), ok=False)
                self.assertIn("Unsafe archive entry", result.stderr)
                self.assertEqual(self.calls("sign"), [])
        self.assertFalse((self.root / "escaped").exists())

    def test_remote_corrupt_archive_is_rejected(self):
        archives = self.remote_archives()
        for archive in archives.iterdir():
            archive.write_text("not a gzip archive")
        self.run_script(self.remote_arguments(), ok=False)
        self.assertEqual(self.calls("sign"), [])

    def test_invalid_remote_options_fail_before_external_commands(self):
        for extra in (["--remote", "guix"], ["--remote-repo", "sv2-tp"],
                      ["--remote", "-bad"], ["--remote", "host;command"]):
            with self.subTest(extra=extra):
                self.run_script([*extra, *self.arguments()], ok=False)
                self.assertEqual(self.calls(), [])
        for host in ("host;command", "user@host:/path", "host with spaces"):
            with self.subTest(host=host):
                args = self.remote_arguments()
                args[args.index("--remote") + 1] = host
                self.run_script(args, ok=False)
                self.assertEqual(self.calls(), [])
        for repo in ("repo with spaces", "repo;command", "-option", "$(command)"):
            with self.subTest(repo=repo):
                self.run_script(self.remote_arguments(repo), ok=False)
                self.assertEqual(self.calls(), [])
        args = self.arguments()
        index = args.index("--input-dir")
        args[index:index + 2] = ["--remote", "guix"]
        self.run_script(args, ok=False)
        self.assertEqual(self.calls(), [])

    def test_invalid_project_options_fail_before_signing(self):
        base = self.arguments([])[:-1]
        for options in (["--project", "unknown", "--release", "1.0.3"],
                        ["--project", "sv2-tp"], ["--release", "1.0.3"],
                        ["--project", "sv2-tp", "--release", "../escape"],
                        ["--project", "sv2-tp", "--release", "1.0.3", self.targets[0]]):
            with self.subTest(options=options):
                self.run_script([*base, *options], ok=False)
                self.assertEqual(self.calls(), [])

    def test_two_architectures_preserve_layout_and_check_each_submission(self):
        self.run_script()
        self.assertEqual([call[1] for call in self.calls()],
                         ["--help", "--find"] + ["sign", "apply", "verify", "notarize", "notarytool"] * 2)
        self.assertEqual(sorted(str(p.relative_to(self.output)) for p in self.output.rglob("*sign")),
                         [self.targets[0] + ".arm64sign", self.targets[1] + ".x86_64sign"])
        for target in self.targets:
            self.assertEqual((self.inputs / target).read_text(), "signed")
        sign = self.calls("sign")[0]
        self.assertEqual(sign[2:6], ["--passphrase", "fake signing secret", "--hardened-runtime", str(self.certificate)])
        notary = self.calls("notarytool")[0]
        self.assertEqual(notary, ["xcrun", "notarytool", "info", SUBMISSION,
                                 "--key", str(self.api_key), "--key-id", "TESTKEY",
                                 "--issuer", "test-issuer", "--output-format", "json"])
        self.assertEqual(self.calls("notarize")[0][3], "fake signing secret")

    def test_universal_binary_applies_both_slices_together(self):
        self.env["SIGN_ARCHES"] = "arm64,x86_64"
        self.run_script(self.arguments(self.targets[:1]))
        self.assertEqual(len(self.calls("apply")), 1)
        self.assertEqual(len(list(self.output.rglob("*sign"))), 2)

    def test_bundle_resources_and_ticket_are_published(self):
        target = "dist/My App.app"
        binary = self.inputs / target / "Contents/MacOS/main"
        binary.parent.mkdir(parents=True)
        binary.write_text("unsigned")
        self.run_script(self.arguments([target]))
        contents = self.output / target / "Contents"
        self.assertEqual((contents / "CodeResources").read_text(), "ticket")
        self.assertEqual((contents / "_CodeSignature/CodeResources").read_text(), "resources")
        self.assertEqual((contents / "MacOS/main.arm64sign").read_text(), "signature")

    def test_distinct_and_empty_passphrases(self):
        self.env.update(SIGNAPPLE_PASSPHRASE="", SIGNAPPLE_NOTARY_PASSPHRASE="fake API secret")
        self.run_script()
        self.assertEqual(self.calls("sign")[0][3], "")
        self.assertEqual(self.calls("notarize")[0][3], "fake API secret")

    def test_shell_tracing_does_not_reveal_secrets(self):
        result = self.run_script(trace=True)
        self.assertNotIn("fake signing secret", result.stdout + result.stderr)

    def test_unencrypted_api_key_gets_explicit_empty_passphrase(self):
        self.env["SIGNAPPLE_NOTARY_PASSPHRASE"] = ""
        self.run_script()
        self.assertEqual(self.calls("notarize")[0][3], "")

    def test_terminal_prompts_once_without_echoing_passphrase(self):
        self.env.pop("SIGNAPPLE_PASSPHRASE")
        master, slave = pty.openpty()
        try:
            with subprocess.Popen([str(SCRIPT), *self.arguments()], cwd=self.root,
                                  env=self.env, stdin=slave, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) as process:
                try:
                    ready, _, _ = select.select([process.stderr], [], [], 10)
                    self.assertTrue(ready, "Timed out waiting for passphrase prompt")
                    prompt = os.read(process.stderr.fileno(), 4096)
                    self.assertEqual(prompt, b"Enter signing key passphrase: ")
                    os.write(master, b"fake terminal secret\n")
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, (stdout, stderr))
                    self.assertNotIn(b"fake terminal secret", stdout + stderr)
                    self.assertNotIn(b"passphrase:", stderr)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
        finally:
            os.close(master)
            os.close(slave)
        self.assertEqual(self.calls("sign")[0][3], "fake terminal secret")
        self.assertEqual(self.calls("notarize")[0][3], "fake terminal secret")
        self.assertEqual(list(self.root.rglob(".sign-apples.*")), [])

    def test_noninteractive_run_requires_passphrase(self):
        self.env.pop("SIGNAPPLE_PASSPHRASE")
        result = self.run_script(ok=False)
        self.assertIn("Set SIGNAPPLE_PASSPHRASE", result.stderr)
        self.assertEqual(self.calls("sign"), [])

    def test_each_external_failure_stops_and_cleans_up(self):
        for command in ("--help", "--find", "sign", "apply", "verify", "notarize", "notarytool"):
            with self.subTest(command=command):
                self.env["FAIL_COMMAND"] = command
                self.log.unlink(missing_ok=True)
                self.run_script(ok=False)
                self.assertEqual(self.calls()[-1][1], command)

    def test_silent_apply_failure_is_caught_before_notarizing(self):
        self.env["APPLY_SILENT_FAIL"] = "1"
        self.run_script(ok=False)
        self.assertEqual(self.calls("notarize"), [])

    def test_failure_on_later_target_does_not_publish_partial_output(self):
        other = self.inputs / "other program"
        other.write_text("unsigned")
        self.env["FAIL_TARGET"] = other.name
        self.run_script(self.arguments([self.targets[0], other.name]), ok=False)
        self.assertEqual(len(self.calls("notarize")), 2)
        self.assertEqual(len(self.calls("notarytool")), 1)

    def test_later_target_failure_preserves_existing_output(self):
        self.output.mkdir()
        (self.output / "previous-release.arm64sign").write_text("existing signature")
        other = self.inputs / "other program"
        other.write_text("unsigned")
        self.env["FAIL_TARGET"] = other.name
        self.run_script(self.arguments([self.targets[0], other.name]), ok=False)
        self.assertEqual(len(self.calls("notarize")), 2)
        self.assertEqual(len(self.calls("notarytool")), 1)

    def test_only_top_level_accepted_json_status_succeeds(self):
        for info in ('{"status":"Invalid"}', '{"status":"In Progress"}',
                     '{"status":"accepted"}', '{"message":"Accepted"}',
                     '{"status":"Invalid","nested":{"status":"Accepted"}}',
                     'not json "status": "Accepted"', '[]', 'null',
                     '"Accepted"', '{"status":42}'):
            with self.subTest(info=info):
                self.env["NOTARY_INFO"] = info
                self.run_script(ok=False)

    def test_bad_or_missing_submission_id_stops_before_status_lookup(self):
        for setting in ({"NO_SUBMISSION": "1"}, {"SUBMISSION": "--help"}):
            with self.subTest(setting=setting):
                self.env.pop("NO_SUBMISSION", None)
                self.env.pop("SUBMISSION", None)
                self.env.update(setting)
                self.log.unlink(missing_ok=True)
                self.run_script(ok=False)
                self.assertEqual(self.calls("notarytool"), [])

    def test_all_targets_are_validated_before_signing(self):
        for target in ("missing", "../outside", "/absolute", "./relative", "a//b", "a/", "a\nb"):
            with self.subTest(target=target):
                self.run_script(self.arguments([self.targets[0], target]), ok=False)
                self.assertEqual(self.calls(), [])

    def test_duplicate_and_overlapping_targets_are_rejected(self):
        bundle = self.inputs / "Example.app"
        bundle.mkdir()
        (bundle / "binary").touch()
        for targets in ([self.targets[0]] * 2, ["Example.app", "Example.app/binary"],
                        ["Example.app/binary", "Example.app"]):
            with self.subTest(targets=targets):
                self.run_script(self.arguments(targets), ok=False)
                self.assertEqual(self.calls(), [])

    def test_symlinks_are_rejected(self):
        (self.inputs / "link").symlink_to(self.inputs / self.targets[0])
        (self.inputs / "linked-dir").symlink_to(self.inputs / "arm64", target_is_directory=True)
        for target in ("link", "linked-dir/pkg/bin/program"):
            with self.subTest(target=target):
                self.run_script(self.arguments([target]), ok=False)
                self.assertEqual(self.calls(), [])

    def test_existing_output_preserves_old_releases_and_publishes_after_success(self):
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("existing signatures")
        old_release = self.output / "arm64/pkg-old/bin/program.arm64sign"
        old_release.parent.mkdir(parents=True)
        old_release.write_text("previous release")
        self.env.update(WATCH_OUTPUT=str(self.output), WATCH_FILES=json.dumps([
            self.targets[0] + ".arm64sign", self.targets[1] + ".x86_64sign"]))
        self.run_script()
        self.assertEqual(marker.read_text(), "existing signatures")
        self.assertEqual(old_release.read_text(), "previous release")
        for target, arch in zip(self.targets, ("arm64", "x86_64")):
            self.assertEqual((self.output / (target + f".{arch}sign")).read_text(), "signature")
        self.assertEqual(len(self.calls("notarytool")), 2)

    def test_conflicting_signature_refuses_entire_publication(self):
        conflict = self.output / (self.targets[1] + ".x86_64sign")
        conflict.parent.mkdir(parents=True)
        conflict.write_text("existing signature for this release")
        result = self.run_script(ok=False)
        self.assertIn("Refusing to overwrite existing output", result.stderr)
        self.assertEqual(len(self.calls("notarytool")), 2)
        self.assertFalse((self.output / (self.targets[0] + ".arm64sign")).exists())

    def test_conflicting_directory_refuses_entire_publication(self):
        self.output.mkdir()
        (self.output / "x86_64").write_text("a file where a directory is needed")
        self.run_script(ok=False)
        self.assertFalse((self.output / "arm64").exists())

    def test_output_parent_symlink_does_not_redirect_signatures(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.output.mkdir()
        (self.output / "x86_64").symlink_to(outside, target_is_directory=True)
        self.run_script(ok=False)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.output / "arm64").exists())

    def test_existing_signature_symlink_is_preserved(self):
        outside = self.root / "existing signature"
        outside.write_text("keep this signature")
        conflict = self.output / (self.targets[1] + ".x86_64sign")
        conflict.parent.mkdir(parents=True)
        conflict.symlink_to(outside)
        self.run_script(ok=False)
        self.assertEqual(outside.read_text(), "keep this signature")

    @unittest.skipIf(os.geteuid() == 0, "Root bypasses directory write permissions")
    def test_publication_copy_failure_rolls_back_only_new_paths(self):
        blocked = self.output / Path(self.targets[1]).parent
        blocked.mkdir(parents=True)
        (self.output / "keep").write_text("existing output")
        blocked.chmod(0o500)
        self.addCleanup(blocked.chmod, 0o755)
        result = self.run_script(ok=False)
        self.assertIn("Could not publish signatures", result.stderr)
        self.assertFalse((self.output / "arm64").exists())
        self.assertEqual(len(self.calls("notarytool")), 2)

    def test_output_file_is_preserved(self):
        self.output.write_text("existing file")
        self.run_script(ok=False)
        self.assertEqual(self.calls(), [])

    def test_output_directory_symlink_is_preserved(self):
        outside = self.root / "existing directory"
        outside.mkdir()
        self.output.symlink_to(outside, target_is_directory=True)
        self.run_script(ok=False)
        self.assertEqual(self.calls(), [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_dangling_output_symlink_is_preserved(self):
        self.output.symlink_to(self.root / "absent")
        self.run_script(ok=False)
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(self.calls(), [])

    def test_output_inside_bundle_is_rejected(self):
        bundle = self.inputs / "Example.app"
        bundle.mkdir()
        self.output = bundle / "signatures"
        self.run_script(self.arguments([bundle.name]), ok=False)
        self.assertEqual(self.calls("sign"), [])

    def test_output_equal_to_bundle_is_rejected(self):
        bundle = self.inputs / "Example.app"
        bundle.mkdir()
        self.output = bundle
        self.run_script(self.arguments([bundle.name]), ok=False)
        self.assertEqual(self.calls("sign"), [])

    def test_unreadable_inputs_and_mismatched_api_key_id(self):
        for path in (self.certificate, self.api_key):
            path.unlink()
            self.run_script(ok=False)
            self.assertEqual(self.calls(), [])
            path.touch()
        args = self.arguments()
        args[args.index("--key-id") + 1] = "WRONGKEY"
        self.run_script(args, ok=False)
        self.assertEqual(self.calls(), [])

    def test_help_version_and_invalid_arguments(self):
        self.assertIn("TARGET", self.run_script(["--help"]).stdout)
        self.assertEqual(self.run_script(["--version"]).stdout.strip(), "sign-apples.sh 1.0")
        for args in ([], ["--bogus"], ["--certificate"], ["--input-dir", ""],
                     self.arguments([])):
            with self.subTest(args=args):
                self.run_script(args, ok=False)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
