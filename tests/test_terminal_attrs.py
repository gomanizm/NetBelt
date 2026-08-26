"""SGR 表示属性の検証。Qt を使わない。"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.attrs import Attr, DEFAULT, apply_sgr   # noqa: E402


class SgrTest(unittest.TestCase):
    def test_no_params_means_full_reset(self):
        dressed = Attr(fg=1, bg=2, bold=True, underline=True, reverse=True)
        self.assertEqual(apply_sgr(dressed, ()), DEFAULT)

    def test_zero_resets_everything(self):
        dressed = Attr(fg=1, bg=2, bold=True, underline=True, reverse=True)
        self.assertEqual(apply_sgr(dressed, (0,)), DEFAULT)

    def test_an_omitted_param_counts_as_reset(self):
        self.assertEqual(apply_sgr(Attr(1, 2, True, False, False), (None,)),
                         DEFAULT)

    def test_bold_and_reverse_pile_up(self):
        attr = apply_sgr(DEFAULT, (1,))
        attr = apply_sgr(attr, (7,))
        self.assertEqual(attr, DEFAULT._replace(bold=True, reverse=True))

    def test_each_attribute_has_its_own_off_switch(self):
        dressed = Attr(None, None, True, True, True)
        self.assertEqual(apply_sgr(dressed, (22,)).bold, False)
        self.assertEqual(apply_sgr(dressed, (24,)).underline, False)
        self.assertEqual(apply_sgr(dressed, (27,)).reverse, False)

    def test_basic_and_bright_colours(self):
        self.assertEqual(apply_sgr(DEFAULT, (31,)).fg, 1)
        self.assertEqual(apply_sgr(DEFAULT, (44,)).bg, 4)
        self.assertEqual(apply_sgr(DEFAULT, (91,)).fg, 9)
        self.assertEqual(apply_sgr(DEFAULT, (104,)).bg, 12)

    def test_default_colour_is_none_not_a_number(self):
        attr = apply_sgr(DEFAULT, (31, 44))
        attr = apply_sgr(attr, (39, 49))
        self.assertIsNone(attr.fg)
        self.assertIsNone(attr.bg)

    def test_many_params_apply_in_order(self):
        attr = apply_sgr(Attr(1, 2, True, False, False), (0, 1, 33, 40))
        self.assertEqual(attr, Attr(3, 0, True, False, False))

    def test_256_palette_via_semicolons(self):
        self.assertEqual(apply_sgr(DEFAULT, (38, 5, 129)).fg, 129)
        self.assertEqual(apply_sgr(DEFAULT, (48, 5, 236)).bg, 236)

    def test_truecolor_via_semicolons(self):
        self.assertEqual(apply_sgr(DEFAULT, (38, 2, 10, 20, 30)).fg,
                         (10, 20, 30))

    def test_extended_colour_consumes_its_own_params_only(self):
        attr = apply_sgr(DEFAULT, (38, 5, 129, 1))
        self.assertEqual(attr.fg, 129)
        self.assertTrue(attr.bold)

    def test_a_broken_extended_colour_drops_the_rest(self):
        # 38 の後が読めない形なら、それ以降まとめて捨てる (適用しない)
        attr = apply_sgr(DEFAULT, (38, 9, 1))
        self.assertEqual(attr, DEFAULT)

    def test_out_of_range_palette_numbers_are_clamped(self):
        self.assertEqual(apply_sgr(DEFAULT, (38, 5, 999)).fg, 255)
        self.assertEqual(apply_sgr(DEFAULT, (38, 2, 300, 300, 300)).fg,
                         (255, 255, 255))

    def test_unknown_codes_are_skipped_silently(self):
        attr = apply_sgr(DEFAULT, (3, 1))   # italic は対応しない
        self.assertEqual(attr, DEFAULT._replace(bold=True))


if __name__ == "__main__":
    unittest.main()
