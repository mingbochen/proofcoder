import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SUPERSEDED_MODULES = ("legacy_util.py", "util.py")


class TextToolsTests(unittest.TestCase):
    def test_exposes_the_helpers_under_the_new_name(self) -> None:
        from text_tools import slugify, titleize

        self.assertEqual(slugify("Release Notes"), "release-notes")
        self.assertEqual(titleize("release-notes"), "Release Notes")

    def test_report_still_builds_anchors_and_headings(self) -> None:
        import report

        self.assertEqual(report.anchor("Release Notes"), "#release-notes")
        self.assertEqual(report.heading("release-notes"), "Release Notes")

    def test_drops_the_superseded_modules(self) -> None:
        for name in SUPERSEDED_MODULES:
            with self.subTest(module=name):
                self.assertFalse(
                    (PROJECT_ROOT / name).exists(),
                    f"{name} is superseded by text_tools.py and should be gone",
                )


if __name__ == "__main__":
    unittest.main()
