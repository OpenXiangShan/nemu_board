#!/usr/bin/env python3
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
GEN = ROOT / "DTSGen.py"


class DTSGenInitrdTests(unittest.TestCase):
    def run_generator(self, *args):
        return subprocess.run(
            [sys.executable, str(GEN), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_defaults_remain_compatible(self):
        dts = self.run_generator()
        self.assertIn("timebase-frequency = <10000000>;", dts)
        self.assertNotIn("linux,initrd-start", dts)
        self.assertNotIn("linux,initrd-end", dts)

    def test_initrd_and_timebase_cli_values_are_emitted(self):
        dts = self.run_generator(
            "--timebase-freq", "1234567",
            "--initrd-start", "0x80600000",
            "--initrd-end", "0x80604567",
        )
        self.assertIn("timebase-frequency = <1234567>;", dts)
        self.assertIn("linux,initrd-start = <0x0 0x80600000>;", dts)
        self.assertIn("linux,initrd-end = <0x0 0x80604567>;", dts)


if __name__ == "__main__":
    unittest.main()
