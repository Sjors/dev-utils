import importlib.util
import io
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    'hwi_targeted', Path(__file__).resolve().parents[1] / 'hwi-run-targeted-device-tests.py',
)
TARGETED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TARGETED)


class FilterTest(unittest.TestCase):
    def test_filters_nested_suites_and_preserves_case_instances(self):
        executed = []

        class GenericCase(unittest.TestCase):
            def test_selected(self):
                executed.append('generic')

            def test_other(self):
                executed.append('unwanted')

        class DeviceSpecificCase(unittest.TestCase):
            def test_selected(self):
                executed.append('device-specific')

        generic = GenericCase('test_selected')
        device = DeviceSpecificCase('test_selected')
        suite = unittest.TestSuite([
            unittest.TestSuite([generic, GenericCase('test_other')]),
            device,
        ])
        filtered = TARGETED.filter_suite(suite, ['test_selected'])
        self.assertEqual(list(filtered), [generic, device])
        result = unittest.TextTestRunner(stream=io.StringIO()).run(filtered)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(executed, ['generic', 'device-specific'])

    def test_rejects_each_unmatched_pattern(self):
        class Example(unittest.TestCase):
            def test_present(self):
                pass

        suite = unittest.TestSuite([Example('test_present')])
        with self.assertRaisesRegex(ValueError, 'test_missing'):
            TARGETED.filter_suite(suite, ['test_present', 'test_missing'])


if __name__ == '__main__':
    unittest.main()
