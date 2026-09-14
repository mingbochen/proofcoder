import unittest

from word_wrap import wrap_words


class WrapWordsTests(unittest.TestCase):
    def test_splits_on_the_width_boundary(self) -> None:
        self.assertEqual(wrap_words("alpha beta gamma", 11), ["alpha beta", "gamma"])

    def test_keeps_the_final_line(self) -> None:
        self.assertEqual(wrap_words("alpha beta", 20), ["alpha beta"])

    def test_rejects_a_zero_width(self) -> None:
        with self.assertRaises(ValueError):
            wrap_words("alpha", 0)


if __name__ == "__main__":
    unittest.main()
