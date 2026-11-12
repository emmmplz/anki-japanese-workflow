import unittest

from tts_service.text_chunking import split_by_length


class CharacterChunkingTests(unittest.TestCase):
    def test_short_sentences_are_kept_together(self):
        text = '今日は晴れです。風も穏やかで、散歩に行きたいです。'
        self.assertEqual(split_by_length(text), [text])

    def test_boundaries_preserve_every_character_and_order(self):
        for size in [1, 149, 150, 151, 299, 300, 301, 1000]:
            with self.subTest(size=size):
                text = ('日a😀、。語' * 170)[:size]
                chunks = split_by_length(text)
                self.assertEqual(''.join(chunks), text)
                self.assertTrue(all(len(chunk) <= 150 for chunk in chunks))
                self.assertTrue(all(len(chunk) == 150 for chunk in chunks[:-1]))

    def test_short_tail_is_not_merged_back(self):
        self.assertEqual([len(c) for c in split_by_length('あ' * 151)], [150, 1])

    def test_newlines_do_not_make_extra_chunks(self):
        self.assertEqual(split_by_length('第一句。\r\n第二句。\n第三句。'), ['第一句。 第二句。 第三句。'])

    def test_invalid_limit_is_rejected(self):
        for limit in [0, -1, 511, True, 1.5, '150']:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                split_by_length('日本語', limit)
