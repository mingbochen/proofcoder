import unittest

import settings
from message import format_message


class MessageTests(unittest.TestCase):
    def test_declares_default_separator(self) -> None:
        self.assertEqual(settings.DEFAULT_SEPARATOR, " | ")

    def test_formats_with_configured_separator(self) -> None:
        self.assertEqual(format_message("ready"), "INFO | ready")


if __name__ == "__main__":
    unittest.main()
