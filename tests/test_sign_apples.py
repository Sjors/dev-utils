"""Exercise the signing workflow with fake tools; no Apple credentials/network."""

import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
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

if tool == "xcrun":
    assert args[:2] == ["notarytool", "info"]
    print(os.environ.get("NOTARY_INFO", '{"status": "Accepted"}'))
elif command == "sign":
    assert args[1] == "--passphrase" and args[3] == "--hardened-runtime"
    target = Path(args[5])
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
        for name in ("signapple", "xcrun"):
            tool = mock_bin / name
            tool.write_text(f"#!{sys.executable}\n" + MOCK_TOOL)
            tool.chmod(0o755)
        self.env = dict(os.environ)
        for name in ("SIGNAPPLE_PASSPHRASE", "SIGNAPPLE_NOTARY_PASSPHRASE",
                     "SIGNAPPLE_CERTIFICATE", "SIGNAPPLE_API_KEY",
                     "SIGNAPPLE_KEY_ID", "SIGNAPPLE_ISSUER",
                     "FAIL_COMMAND", "NOTARY_INFO", "APPLY_SILENT_FAIL",
                     "NO_SUBMISSION", "SUBMISSION", "SIGN_ARCHES", "FAIL_TARGET",
                     "WATCH_OUTPUT", "WATCH_FILES"):
            self.env.pop(name, None)
        self.env.update(PATH=str(mock_bin) + os.pathsep + self.env["PATH"],
                        TOOL_LOG=str(self.log), SIGNAPPLE_PASSPHRASE="fake signing secret")
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

    def run_script(self, args=None, ok=True, trace=False):
        original_output = self.output_state()
        command = ["/bin/bash", "-x", str(SCRIPT)] if trace else [str(SCRIPT)]
        result = subprocess.run(
            [*command, *(self.arguments() if args is None else args)],
            cwd=self.root, env=self.env, input="", capture_output=True, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
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
