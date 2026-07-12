import tempfile
import sys
import unittest
from pathlib import Path


API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))


class FileRangeTest(unittest.TestCase):
    def test_range_iterator_never_allocates_the_whole_range(self):
        from podcast.file_stream import iter_file_range, parse_single_range

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audio.bin"
            path.write_bytes(bytes(range(256)) * 1024)
            start, end = parse_single_range("bytes=100-200000", path.stat().st_size)
            chunks = list(iter_file_range(path, start, end, chunk_size=4096))

        self.assertTrue(chunks)
        self.assertTrue(all(0 < len(chunk) <= 4096 for chunk in chunks))
        self.assertEqual(sum(map(len, chunks)), end - start + 1)

    def test_range_parser_supports_open_and_suffix_ranges(self):
        from podcast.file_stream import parse_single_range

        self.assertEqual(parse_single_range("bytes=10-", 100), (10, 99))
        self.assertEqual(parse_single_range("bytes=-10", 100), (90, 99))
        with self.assertRaises(ValueError):
            parse_single_range("bytes=0-1,4-5", 100)
