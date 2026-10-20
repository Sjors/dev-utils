#!/usr/bin/env python3
"""Fallback for branches without run_tests.py's native --test option."""

import argparse
from fnmatch import fnmatchcase
from pathlib import Path
import sys
import unittest


DEVICES = (
    "bitbox01",
    "bitbox02",
    "coldcard",
    "coldcard-edge",
    "jade",
    "keepkey",
    "ledger",
    "ledger-legacy",
    "trezor-1",
    "trezor-t",
)


def get_runner(device):
    if device == "bitbox01":
        from test_digitalbitbox import digitalbitbox_test_suite
        return digitalbitbox_test_suite
    if device == "bitbox02":
        from test_bitbox02 import bitbox02_test_suite
        return bitbox02_test_suite
    if device in ("coldcard", "coldcard-edge"):
        from test_coldcard import coldcard_test_suite
        return lambda simulator, bitcoind, interface: coldcard_test_suite(
            simulator,
            bitcoind,
            interface,
            is_edge=device == "coldcard-edge",
        )
    if device == "jade":
        from test_jade import jade_test_suite
        return jade_test_suite
    if device == "keepkey":
        from test_keepkey import keepkey_test_suite
        return keepkey_test_suite
    if device in ("ledger", "ledger-legacy"):
        from test_ledger import ledger_test_suite
        return lambda simulator, bitcoind, interface: ledger_test_suite(
            simulator,
            bitcoind,
            interface,
            legacy=device == "ledger-legacy",
        )
    if device in ("trezor-1", "trezor-t"):
        from test_trezor import trezor_test_suite
        return lambda simulator, bitcoind, interface: trezor_test_suite(
            simulator,
            bitcoind,
            interface,
            "1" if device == "trezor-1" else "t",
        )
    raise ValueError(f"Unsupported device: {device}")


def filter_suite(suite, patterns):
    """Filter assembled suites, including device-specific TestCase subclasses."""
    selected = unittest.TestSuite()
    matched = set()

    def cases(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                yield from cases(child)
        else:
            yield item

    for case in cases(suite):
        matches = [pattern for pattern in patterns if fnmatchcase(case._testMethodName, pattern)]
        if matches:
            matched.update(matches)
            selected.addTest(case)
    missing = [pattern for pattern in patterns if pattern not in matched]
    if missing:
        raise ValueError(f"no test methods matched: {', '.join(missing)}")
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device", choices=DEVICES)
    parser.add_argument("tests", nargs="+", help="Exact method names or quoted method-name globs")
    parser.add_argument("--simulator", required=True)
    parser.add_argument("--bitcoind", required=True)
    parser.add_argument("--interface", choices=("library", "cli", "bindist", "stdin"), default="library")
    args = parser.parse_args()
    test_dir = Path.cwd()
    if not (test_dir / "test_device.py").is_file():
        parser.error("run this helper from the HWI test directory")
    sys.path.insert(0, str(test_dir))
    from test_device import Bitcoind

    original_run = unittest.TextTestRunner.run
    original_stderr = sys.stderr

    def run_selected(runner, suite):
        return original_run(runner, filter_suite(suite, args.tests))

    try:
        unittest.TextTestRunner.run = run_selected
        bitcoind = Bitcoind.create(args.bitcoind)
        success = get_runner(args.device)(args.simulator, bitcoind, args.interface)
    except ValueError as error:
        sys.stderr = original_stderr
        parser.error(str(error))
    finally:
        unittest.TextTestRunner.run = original_run
        sys.stderr = original_stderr
    return not success


if __name__ == "__main__":
    sys.exit(main())
