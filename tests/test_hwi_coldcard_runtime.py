import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "hwi_coldcard_runtime", Path(__file__).resolve().parents[1] / "hwi-coldcard-runtime.py",
)
RUNTIME = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNTIME)


class ColdcardRuntimeTest(unittest.TestCase):
    def test_runtime_is_independent_and_does_not_copy_policy_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "firmware"
            (source / "unix/work/settings").mkdir(parents=True)
            (source / "shared").mkdir()
            (source / "unix/simulator.py").write_text("# simulator\n")
            binary = root / "native-binary"
            binary.write_bytes(b"native simulator")
            binary.chmod(0o755)
            (source / "unix/coldcard-mpy").symlink_to(binary)
            generated = root / "generated.py"
            generated.write_text("ANSWER = 42\n")
            (source / "shared/generated.py").symlink_to(generated)
            (source / "unix/work/settings/existing.aes").write_text("saved policy")
            (source / "hwi-runtime.json").write_text(json.dumps({"commit": "upstream-pin"}))

            first = root / "runtime-one"
            RUNTIME.stage(source, first)
            binary.unlink()
            generated.unlink()
            self.assertEqual((first / "unix/coldcard-mpy").read_bytes(), b"native simulator")
            self.assertFalse((first / "unix/coldcard-mpy").is_symlink())
            self.assertEqual((first / "shared/generated.py").read_text(), "ANSWER = 42\n")
            self.assertEqual(list((first / "unix/work/settings").iterdir()), [])
            (first / "unix/work/settings/new.aes").write_text("new policy")

            alias = root / "alias"
            alias.symlink_to(first / "unix", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "outside the source tree"):
                RUNTIME.stage(first, alias / "nested-runtime")

            second = root / "runtime-two"
            RUNTIME.stage(first, second)
            self.assertEqual(list((second / "unix/work/settings").iterdir()), [])
            (second / "unix/coldcard-mpy").write_bytes(b"changed")
            self.assertEqual((first / "unix/coldcard-mpy").read_bytes(), b"native simulator")
            metadata = json.loads((second / "hwi-runtime.json").read_text())
            self.assertEqual(metadata["commit"], "upstream-pin")

            with self.assertRaisesRegex(ValueError, "already exists"):
                RUNTIME.stage(first, second)
            self.assertEqual((second / "unix/coldcard-mpy").read_bytes(), b"changed")

    def test_missing_binary_does_not_create_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "unix").mkdir(parents=True)
            (source / "unix/simulator.py").write_text("# source only\n")
            destination = root / "runtime"
            with self.assertRaisesRegex(ValueError, "missing simulator component"):
                RUNTIME.stage(source, destination)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
