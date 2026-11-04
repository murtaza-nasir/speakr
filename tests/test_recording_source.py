import os
import tempfile
import unittest
from unittest.mock import MagicMock
from io import BytesIO
from werkzeug.datastructures import FileStorage

from src.services.recording_source import (
    FileStorageSource,
    PathSource,
    coerce_source,
)


class TestFileStorageSource(unittest.TestCase):
    def test_delegates(self):
        fs = FileStorage(stream=BytesIO(b'hello'), filename='a.mp3')
        src = FileStorageSource(fs)
        self.assertEqual(src.filename, 'a.mp3')
        self.assertEqual(src.tell(), 0)
        fs.seek(0, 2)
        self.assertEqual(src.tell(), 5)
        src.seek(0)
        dst = tempfile.mktemp()
        src.save(dst)
        with open(dst, 'rb') as f:
            self.assertEqual(f.read(), b'hello')
        os.remove(dst)


class TestPathSource(unittest.TestCase):
    def test_delegates(self):
        fd, path = tempfile.mkstemp()
        os.write(fd, b'world')
        os.close(fd)
        try:
            src = PathSource(path)
            self.assertEqual(src.filename, os.path.basename(path))
            src.seek(0, 2)
            self.assertEqual(src.tell(), 5)
            src.seek(0)
            dst = tempfile.mktemp()
            src.save(dst)
            with open(dst, 'rb') as f:
                self.assertEqual(f.read(), b'world')
            os.remove(dst)
            src.close()
        finally:
            os.remove(path)


class TestCoerceSource(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(coerce_source())

    def test_filestorage_wrapped(self):
        fs = FileStorage(stream=BytesIO(b'x'), filename='x.mp3')
        src = coerce_source(uploaded_file=fs)
        self.assertIsInstance(src, FileStorageSource)

    def test_path_source_passthrough(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            ps = PathSource(path)
            self.assertIs(coerce_source(source=ps), ps)
        finally:
            os.remove(path)

    def test_source_wins_over_uploaded(self):
        fs = FileStorage(stream=BytesIO(b'x'), filename='x.mp3')
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            ps = PathSource(path)
            self.assertIs(coerce_source(uploaded_file=fs, source=ps), ps)
        finally:
            os.remove(path)

    def test_raw_filestorage_as_source_wrapped(self):
        """A raw FileStorage passed as ``source`` is wrapped like the other
        input form, so every caller sees the same adapter."""
        fs = FileStorage(stream=BytesIO(b'x'), filename='x.mp3')
        self.assertIsInstance(coerce_source(source=fs), FileStorageSource)

    def test_unsupported_source_rejected(self):
        with self.assertRaises(TypeError):
            coerce_source(source=object())
        with self.assertRaises(TypeError):
            coerce_source(uploaded_file=object())


if __name__ == '__main__':
    unittest.main()
