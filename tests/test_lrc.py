import unittest

from dashboard.apple_music_lyrics_backend.lyrics import find_current_line, parse_lrc


class ParseLRCTests(unittest.TestCase):
    def test_parses_fraction_variants_multiple_timestamps_and_offset(self):
        content = """[ar:Example Artist]
[offset:250]
[00:01.5]First
[00:02.34][00:03.345]Repeat
[00:04.00]
not timestamped metadata
"""

        lines = parse_lrc(content)

        self.assertEqual([line.text for line in lines], ["First", "Repeat", "Repeat", ""])
        self.assertEqual(
            [round(line.time, 3) for line in lines],
            [1.75, 2.59, 3.595, 4.25],
        )

    def test_finds_current_line_at_boundaries(self):
        lines = parse_lrc("[00:01.00]one\n[00:03.00]three\n[00:05.00]five")

        self.assertEqual(find_current_line(lines, 0.99), -1)
        self.assertEqual(find_current_line(lines, 1.0), 0)
        self.assertEqual(find_current_line(lines, 4.99), 1)
        self.assertEqual(find_current_line(lines, 99.0), 2)


if __name__ == "__main__":
    unittest.main()
