import ctypes
import os
import struct
import tempfile
import unittest
from unittest.mock import patch

from desktop_pet.attachments import AttachmentStore, _dib_as_bmp
from desktop_pet.shell import clipboard


class ClipboardBackendTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "native Win32 DLL binding smoke")
    def test_native_dll_symbols_resolve_without_clipboard_access(self):
        user32, shell32, kernel32 = clipboard._windows_api()
        self.assertTrue(user32.OpenClipboard)
        self.assertTrue(shell32.DragQueryFileW)
        self.assertTrue(kernel32.GlobalLock)

    def test_dib_headers_and_dimension_bound(self):
        for height in (2, -2):
            dib = struct.pack("<IiiHHIIiiII", 40, 2, height, 1, 24, 0, 16, 0, 0, 0, 0) + bytes(16)
            bmp = _dib_as_bmp(dib)
            self.assertEqual(bmp[:2], b"BM")
            self.assertEqual(struct.unpack_from("<I", bmp, 10)[0], 54)
        huge = struct.pack("<IiiHHIIiiII", 40, 10000, 10000, 1, 24, 0, 0, 0, 0, 0, 0)
        with self.assertRaises(ValueError):
            _dib_as_bmp(huge)
        with self.assertRaises(ValueError):
            _dib_as_bmp(struct.pack("<IiiHHIIiiII", 40, -1, 1, 1, 24, 0, 4, 0, 0, 0, 0) + bytes(4))

    def test_clipboard_dib_variants_decode_to_expected_pixels(self):
        from PIL import Image
        # One-pixel-wide rows occupy four bytes; positive height stores bottom row first.
        header24 = lambda h: struct.pack("<IiiHHIIiiII", 40, 1, h, 1, 24, 0, 8, 0, 0, 0, 0)
        dib_cases = [
            ("dib", header24(2) + bytes((255, 0, 0, 0, 0, 0, 255, 0)), ("red", "blue")),
            ("dib", header24(-2) + bytes((0, 0, 255, 0, 255, 0, 0, 0)), ("red", "blue")),
            ("dib", struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 32, 0, 4, 0, 0, 0, 0) + bytes((30, 20, 10, 255)), ("rgb",)),
        ]
        # BITMAPV5HEADER, BI_BITFIELDS with explicit RGBA channel masks.
        v5 = (struct.pack("<IiiHHIIiiII", 124, 1, 1, 1, 32, 3, 4, 0, 0, 0, 0)
              + struct.pack("<IIII", 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
              + bytes(68) + bytes((30, 20, 10, 40)))
        dib_cases.append(("dibv5", v5, ("rgba",)))
        with tempfile.TemporaryDirectory() as tmp:
            store = AttachmentStore(tmp)
            for index, (kind, dib, expected) in enumerate(dib_cases):
                image = store.import_bytes(dib, image_format=kind)
                with Image.open(__import__("io").BytesIO(image.png_bytes)) as decoded:
                    self.assertEqual(decoded.size, (1, 2) if index < 2 else (1, 1))
                    self.assertEqual(decoded.getpixel((0, 0))[:3], (255, 0, 0) if index < 2 else (10, 20, 30))
                    if index < 2:
                        self.assertEqual(decoded.getpixel((0, 1))[:3], (0, 0, 255))
                    if kind == "dibv5":
                        self.assertEqual(decoded.getpixel((0, 0))[3], 40)

    def test_import_bytes_persists_content_addressed_png(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            source = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            source.close()
            try:
                Image.new("RGB", (12, 8), "red").save(source.name)
                with open(source.name, "rb") as stream:
                    payload = stream.read()
                image = AttachmentStore(tmp).import_bytes(payload, image_format="png")
                self.assertEqual(image.image_id, __import__("hashlib").sha256(image.png_bytes).hexdigest())
                self.assertTrue((__import__("pathlib").Path(tmp) / f"{image.image_id}.png").is_file())
                self.assertNotIn("png_bytes", repr(image))
            finally:
                __import__("os").unlink(source.name)

    def test_clipboard_releases_handle_and_clipboard_on_error(self):
        backing = ctypes.create_string_buffer(struct.pack("<Iii", 40, 0, 0) + bytes(28))
        calls = []
        class API:
            def OpenClipboard(self, owner): return 1
            def CloseClipboard(self): calls.append("close")
            def IsClipboardFormatAvailable(self, fmt): return int(fmt == clipboard.CF_DIB)
            def GetClipboardData(self, fmt): return 99
            def RegisterClipboardFormatW(self, name): return 49300
            def GlobalSize(self, handle): return len(backing.raw)
            def GlobalLock(self, handle): return ctypes.addressof(backing)
            def GlobalUnlock(self, handle): calls.append("unlock"); return 1
            def DragQueryFileW(self, *args): return 0
        with patch.object(clipboard, "_windows_api", return_value=(API(), API(), API())):
            with self.assertRaises(ValueError):
                clipboard.read_clipboard()
        self.assertIn("unlock", calls)
        self.assertIn("close", calls)

    def test_snapshot_collects_paths_png_and_normalized_unicode_text(self):
        from PIL import Image
        import io
        png_stream = io.BytesIO()
        Image.new("RGB", (1, 1), "red").save(png_stream, format="PNG")
        buffers = {
            1: ctypes.create_string_buffer(png_stream.getvalue()),
            2: ctypes.create_string_buffer("第一行\r\n第二行\0".encode("utf-16-le")),
        }
        calls = []
        class User:
            def OpenClipboard(self, owner): return 1
            def CloseClipboard(self): calls.append("close")
            def IsClipboardFormatAvailable(self, fmt): return int(fmt in {clipboard.CF_HDROP, 50000, clipboard.CF_UNICODETEXT})
            def GetClipboardData(self, fmt): return {clipboard.CF_HDROP: 3, 50000: 1, clipboard.CF_UNICODETEXT: 2}[fmt]
            def RegisterClipboardFormatW(self, name): return 50000
        class Shell:
            def DragQueryFileW(self, drop, index, buffer, size):
                if index == 0xFFFFFFFF: return 1
                path = "C:\\screens\\截圖.png"
                if buffer is None: return len(path)
                buffer.value = path
                return len(path)
        class Kernel:
            def GlobalSize(self, handle): return len(buffers[handle].raw)
            def GlobalLock(self, handle): return ctypes.addressof(buffers[handle])
            def GlobalUnlock(self, handle): calls.append("unlock"); return 1
        with patch.object(clipboard, "_windows_api", return_value=(User(), Shell(), Kernel())):
            result = clipboard.read_clipboard()
        self.assertEqual(result.file_paths, ("C:\\screens\\截圖.png",))
        self.assertEqual(result.image_format, "png")
        self.assertEqual(result.image_bytes, png_stream.getvalue())
        self.assertEqual(result.text, "第一行\n第二行")
        self.assertIn("close", calls)


if __name__ == "__main__":
    unittest.main()
