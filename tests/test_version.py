"""Paketversion und pyproject.toml müssen zusammenpassen (beim Hochzählen beide Stellen ändern)."""

import os
import re
import unittest

import bindu

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class VersionTests(unittest.TestCase):
    def test_pyproject_matches_package(self):
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
            m = re.search(r'^version = "([^"]+)"', fh.read(), re.M)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), bindu.__version__)


if __name__ == "__main__":
    unittest.main()
