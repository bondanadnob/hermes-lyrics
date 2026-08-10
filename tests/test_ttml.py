import json
import unittest

from dashboard.apple_music_lyrics_backend.ttml import parse_time, parse_ttml


class ParseTTMLTests(unittest.TestCase):
    def test_parses_namespaced_lines_and_word_timing(self):
        content = """<?xml version="1.0" encoding="UTF-8"?>
<tt xmlns="http://www.w3.org/ns/ttml">
  <body><div>
    <p begin="00:00:01.000" end="3s">
      <span begin="1s" end="1500ms">Hello </span>
      <span begin="1.5s" end="2.25s">world</span>
    </p>
    <p begin="3s" end="5s">Plain line</p>
  </div></body>
</tt>
"""

        document = parse_ttml(content)

        self.assertTrue(document.synced)
        self.assertEqual(document.word_timing, "exact")
        self.assertEqual([line.text for line in document.lines], ["Hello world", "Plain line"])
        self.assertEqual(document.lines[0].time, 1.0)
        self.assertEqual(
            [(word.text, word.start, word.end) for word in document.lines[0].words],
            [("Hello ", 1.0, 1.5), ("world", 1.5, 2.25)],
        )
        self.assertEqual(document.lines[1].time, 3.0)
        self.assertEqual(document.lines[1].words, ())

    def test_non_finite_times_are_rejected_and_output_is_json_safe(self):
        self.assertIsNone(parse_time("NaNs"))
        self.assertIsNone(parse_time("Infinitys"))
        content = """<tt><body><div>
          <p begin="1s" end="Infinitys"><span begin="NaNs">bad</span>safe</p>
        </div></body></tt>"""

        document = parse_ttml(content)

        json.dumps(document.to_dict(), allow_nan=False)
        self.assertTrue(document.synced)
        self.assertEqual(document.lines[0].time, 1.0)
        self.assertIsNone(document.lines[0].end)
        self.assertEqual(document.lines[0].words, ())

    def test_untimed_paragraphs_remain_plain_unsynchronized_lyrics(self):
        content = "<tt><body><div><p>First</p><p>Second</p></div></body></tt>"

        document = parse_ttml(content)

        self.assertFalse(document.synced)
        self.assertEqual([line.text for line in document.lines], ["First", "Second"])
        self.assertEqual([line.time for line in document.lines], [0.0, 1.0])
        self.assertTrue(all(not line.words for line in document.lines))

    def test_rejects_external_entities(self):
        content = '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><tt><body><p begin="0s">&xxe;</p></body></tt>'

        with self.assertRaises(ValueError):
            parse_ttml(content)


if __name__ == "__main__":
    unittest.main()
