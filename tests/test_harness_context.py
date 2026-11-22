"""Native status examples from pinned upstream formatters, no TUI/API calls."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.harness_context import parse_context


class NativeContext(unittest.TestCase):
    def test_dsh_compact_and_full_same_reading(self):
        for footer in ('thinking high · ctx 19% (24k/128k)',
                       'thinking high · ctx 24k/128k (19%)'):
            with self.subTest(footer=footer):
                self.assertEqual(parse_context(footer, 'dsh'),
                                 {'ctx_used': 19, 'ctx_window': '128K'})

    def test_dsh_fractional_and_overflow_do_not_use_token_ratio(self):
        self.assertEqual(parse_context('ctx 0.9% (988/128k)', 'dsh')['ctx_used'], 1)
        self.assertEqual(parse_context('ctx 110% (141k/128k)', 'dsh')['ctx_used'], 110)

    def test_hermes_wide_plain_and_styled(self):
        for footer in ('☤ Qwen │ 24k/128K │ 19% │ ◎ 88.0% │ ⏱ 1m',
                       '☤ Qwen │ ~24k/128K pinned │ [██░░░░░░░░] ~19% │ ◎ 88.0%'):
            with self.subTest(footer=footer):
                self.assertEqual(parse_context(footer, 'hermes'),
                                 {'ctx_used': 19, 'ctx_window': '128K'})

    def test_ansi_and_current_footer_wins(self):
        text = 'ctx 90% (115k/128k)\nanswer\n\x1b[38;2;1;2;3mctx 2.0% (2.6k/128k)\x1b[0m'
        self.assertEqual(parse_context(text, 'dsh')['ctx_used'], 2)

    def test_unknown_not_zero_or_other_vendor_parser(self):
        cases = [('ctx 19% (24k/128k)', 'claude'),
                 ('ctx: 33% used\nContext 67% left\n(1M context)', 'dsh'),
                 ('ctx --', 'dsh'), ('ctx 10% (1k/0)', 'dsh'),
                 ('☤ model · 20% · ◎ 90%', 'hermes'),
                 ('☤ model │ ctx -- │ --', 'hermes'),
                 ('ctx 19% (24k/128k)' + '\nanswer' * 13, 'dsh'),
                 ('Σ1M tokens · cache 99%', 'hermes'),
                 ('24k/128K 19%', 'hermes')]
        for text, program in cases:
            with self.subTest(text=text, program=program):
                self.assertEqual(parse_context(text, program),
                                 {'ctx_used': None, 'ctx_window': None})


if __name__ == '__main__':
    unittest.main()
