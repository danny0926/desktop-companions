"""Explicit, bounded snapshots of the native Windows clipboard."""
from __future__ import annotations

import ctypes
import time


MAX_FILES = 32
MAX_TEXT_BYTES = 4_000_000
MAX_PNG_BYTES = 12_000_000
MAX_DIB_BYTES = 160_004_096
CF_UNICODETEXT, CF_HDROP, CF_DIB, CF_DIBV5 = 13, 15, 8, 17


class ClipboardContent:
    __slots__ = ("image_bytes", "image_format", "file_paths", "text")

    def __init__(self, image_bytes: bytes | None = None, image_format: str | None = None,
                 file_paths: tuple[str, ...] = (), text: str | None = None):
        self.image_bytes = image_bytes
        self.image_format = image_format
        self.file_paths = tuple(file_paths)
        self.text = text

    def __repr__(self) -> str:
        return (f"ClipboardContent(image_format={self.image_format!r}, file_count={len(self.file_paths)}, "
                f"has_image={self.image_bytes is not None}, has_text={self.text is not None})")


def _windows_api():
    if not hasattr(ctypes, "WinDLL"):
        raise ValueError("目前平台不支援讀取 Windows 剪貼簿。")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_int
    user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    user32.IsClipboardFormatAvailable.restype = ctypes.c_int
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
    user32.RegisterClipboardFormatW.restype = ctypes.c_uint
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
    shell32.DragQueryFileW.restype = ctypes.c_uint
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    return user32, shell32, kernel32


def _trim_png(payload: bytes) -> bytes:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("剪貼簿 PNG 標頭無效；草稿已保留。")
    position = 8
    while position + 12 <= len(payload):
        length = int.from_bytes(payload[position:position + 4], "big")
        end = position + 12 + length
        if end > len(payload):
            break
        if payload[position + 4:position + 8] == b"IEND" and length == 0:
            return payload[:end]
        position = end
    raise ValueError("剪貼簿 PNG 資料不完整；草稿已保留。")


def read_clipboard(owner_hwnd: int | None = None) -> ClipboardContent:
    """Copy text, image, and copied-file paths; never changes clipboard data."""
    user32, shell32, kernel32 = _windows_api()
    opened = False
    for attempt in range(4):
        if user32.OpenClipboard(ctypes.c_void_p(owner_hwnd) if owner_hwnd else None):
            opened = True
            break
        if attempt < 3:
            time.sleep(0.025 * (attempt + 1))
    if not opened:
        raise ValueError("剪貼簿目前忙碌，請稍後再貼上；草稿已保留。")

    def get_bytes(fmt: int, limit: int, *, minimum: int = 1) -> bytes | None:
        if not user32.IsClipboardFormatAvailable(fmt):
            return None
        handle = user32.GetClipboardData(fmt)
        if not handle:
            raise ValueError("無法讀取剪貼簿內容；草稿已保留。")
        size = kernel32.GlobalSize(handle)
        if size < minimum or size > limit:
            raise ValueError("剪貼簿內容超過大小限制；草稿已保留。")
        address = kernel32.GlobalLock(handle)
        if not address:
            raise ValueError("無法鎖定剪貼簿內容；草稿已保留。")
        try:
            return ctypes.string_at(address, size)
        finally:
            kernel32.GlobalUnlock(handle)

    try:
        paths: tuple[str, ...] = ()
        if user32.IsClipboardFormatAvailable(CF_HDROP):
            drop = user32.GetClipboardData(CF_HDROP)
            count = shell32.DragQueryFileW(drop, 0xFFFFFFFF, None, 0)
            if count > MAX_FILES:
                raise ValueError("剪貼簿檔案數量超過 32 個限制；草稿已保留。")
            collected = []
            for index in range(count):
                length = shell32.DragQueryFileW(drop, index, None, 0)
                if not length or length > 32767:
                    raise ValueError("剪貼簿檔案路徑無效；草稿已保留。")
                buffer = ctypes.create_unicode_buffer(length + 1)
                if shell32.DragQueryFileW(drop, index, buffer, length + 1) != length:
                    raise ValueError("無法讀取剪貼簿檔案路徑；草稿已保留。")
                collected.append(buffer.value)
            paths = tuple(collected)

        png_format = user32.RegisterClipboardFormatW("PNG")
        image_bytes = image_format = None
        # Paths take import priority in the UI, but snapshot all available fields.
        if user32.IsClipboardFormatAvailable(png_format):
            image_bytes = _trim_png(get_bytes(png_format, MAX_PNG_BYTES, minimum=8))
            image_format = "png"
        else:
            fmt = CF_DIBV5 if user32.IsClipboardFormatAvailable(CF_DIBV5) else CF_DIB
            if user32.IsClipboardFormatAvailable(fmt):
                handle = user32.GetClipboardData(fmt)
                if not handle:
                    raise ValueError("無法讀取剪貼簿圖片；草稿已保留。")
                size = kernel32.GlobalSize(handle)
                if size < 40 or size > MAX_DIB_BYTES:
                    raise ValueError("剪貼簿圖片超過大小限制；草稿已保留。")
                address = kernel32.GlobalLock(handle)
                if not address:
                    raise ValueError("無法鎖定剪貼簿圖片；草稿已保留。")
                try:
                    header = ctypes.string_at(address, 40)
                    import struct
                    header_size, width, height = struct.unpack_from("<Iii", header)
                    if header_size < 40 or header_size > size or not width or not height or abs(width) * abs(height) > 40_000_000:
                        raise ValueError("剪貼簿圖片尺寸無效或超過 4000 萬像素；草稿已保留。")
                    image_bytes = ctypes.string_at(address, size)
                finally:
                    kernel32.GlobalUnlock(handle)
                image_format = "dibv5" if fmt == CF_DIBV5 else "dib"

        text = None
        if user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            raw = get_bytes(CF_UNICODETEXT, MAX_TEXT_BYTES + 2, minimum=2)
            text = raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not paths and image_bytes is None and text is None:
            raise ValueError("剪貼簿沒有可貼上的文字、圖片或檔案。")
        return ClipboardContent(image_bytes, image_format, paths, text)
    finally:
        user32.CloseClipboard()
