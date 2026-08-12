"""Bridge path checks must use resolved relative_to, not startswith."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sy.bridge.classifier import check_path_condition


class ProjectPathTests(unittest.TestCase):
    def test_in_project(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "src" / "a.py"
            target.parent.mkdir()
            target.write_text("x", encoding="utf-8")
            self.assertTrue(
                check_path_condition("in_project", str(target), str(root), str(root))
            )

    def test_sibling_prefix_not_in_project(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "proj"
            sibling = Path(raw) / "proj-evil" / "x.py"
            root.mkdir()
            sibling.parent.mkdir()
            sibling.write_text("x", encoding="utf-8")
            self.assertFalse(
                check_path_condition("in_project", str(sibling), str(root), str(root))
            )


class SystemPathTests(unittest.TestCase):
    def test_etc_is_system(self):
        self.assertTrue(check_path_condition("is_system_path", "/etc/passwd", "/", "/"))


if __name__ == "__main__":
    unittest.main()
