import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    'hwi_prereqs', Path(__file__).resolve().parents[1] / 'hwi-check-prereqs.py',
)
PREREQS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREREQS)


class PrerequisiteTest(unittest.TestCase):
    def test_accepts_versioned_and_multiarch_packages_only_when_installed(self):
        installed = PREREQS.parse_installed(
            'libclang-21-dev:amd64\tinstalled\n'
            'libsdl2-dev:amd64\tunpacked\n'
            'libslirp-dev\tconfig-files\n'
            'qemu-user\tinstalled\n'
        )
        required = [('libclang-dev', 'libclang-[0-9]*-dev'),
                    ('qemu-user-static', 'qemu-user'), ('libsdl2-dev',), ('libslirp-dev',)]
        self.assertEqual(PREREQS.missing_requirements(required, installed),
                         [('libsdl2-dev',), ('libslirp-dev',)])

    def test_selects_available_distro_alternative_and_reports_unavailable_packages(self):
        available, unavailable = PREREQS.install_packages(
            [('qemu-user-static', 'qemu-user'), ('gcc-12',),
             ('libclang-dev', 'libclang-[0-9]*-dev')],
            lambda name: name in {'qemu-user', 'libclang-dev'},
        )
        self.assertEqual(available, ['libclang-dev', 'qemu-user'])
        self.assertEqual(unavailable, [('gcc-12',)])

    def test_failed_inventory_is_an_error_not_a_successful_check(self):
        with patch.object(PREREQS.subprocess, 'run', side_effect=OSError('dpkg unavailable')):
            with patch('sys.stderr', new_callable=io.StringIO) as output:
                self.assertEqual(PREREQS.main(['--device', 'trezor-t']), 2)
        self.assertIn('dpkg unavailable', output.getvalue())

    def test_installed_packages_do_not_hide_missing_ledger_executable(self):
        installed = {group[0] for group in PREREQS.requirements(['ledger'])}
        result = type('Inventory', (), {'stdout': ''.join(
            name + '\tinstalled\n' for name in installed)})()
        with patch.object(PREREQS.subprocess, 'run', return_value=result):
            with patch.object(PREREQS.shutil, 'which', return_value=None):
                with patch('sys.stdout', new_callable=io.StringIO) as output:
                    self.assertEqual(PREREQS.main(['--device', 'ledger']), 1)
        self.assertIn('qemu-arm-static in PATH', output.getvalue())

    def test_selected_device_does_not_require_unrelated_toolchains(self):
        ledger = PREREQS.requirements(['ledger-legacy'])
        self.assertIn(('gcc-arm-linux-gnueabihf',), ledger)
        self.assertNotIn(('gcc-12',), ledger)
        self.assertNotIn(('libslirp-dev',), ledger)
        self.assertEqual(PREREQS.requirements(['ledger', 'ledger-legacy']), ledger)
        self.assertIn(('gcc-12',), PREREQS.requirements(['trezor-1']))


if __name__ == '__main__':
    unittest.main()
