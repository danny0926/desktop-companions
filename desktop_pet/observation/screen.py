"""Capture one visible foreground region with Win32 and the Python stdlib.

No screen recorder, timer, OCR, image library or model is started here. The
existing companion worker chooses when to request a single observation. Native
GDI scales before allocating pixel memory, including on large/multiple displays.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes as c
from ctypes import wintypes as w
import hashlib
import os
import struct
import sys
import threading
import time
from typing import Any, Callable
import uuid
import zlib

from desktop_pet.contracts import SCREEN_SCOPES, ScreenObservation


@dataclass(frozen=True)
class _CaptureTarget:
    hwnd: int
    pid: int
    bounds: tuple[int, int, int, int]
    token: str


class _MONITORINFO(c.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT),
                ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


class _BITMAPINFOHEADER(c.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
                ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
                ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]


def _png_from_bgra(pixels: bytes, width: int, height: int) -> bytes:
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = pixels[2::4], pixels[1::4], pixels[0::4]
    rows = b"".join(b"\0" + rgb[row * width * 3:(row + 1) * width * 3]
                    for row in range(height))

    def chunk(kind: bytes, value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 4)) + chunk(b"IEND", b""))


class _Win32Capture:
    source = "win32"

    def __init__(self) -> None:
        self.user32 = c.WinDLL("user32", use_last_error=True)
        self.gdi32 = c.WinDLL("gdi32", use_last_error=True)
        for library, name, arguments, returns in (
            (self.user32, "GetForegroundWindow", [], w.HWND),
            (self.user32, "GetWindowThreadProcessId", [w.HWND, c.POINTER(w.DWORD)], w.DWORD),
            (self.user32, "GetWindowRect", [w.HWND, c.POINTER(w.RECT)], w.BOOL),
            (self.user32, "GetWindowTextW", [w.HWND, w.LPWSTR, c.c_int], c.c_int),
            (self.user32, "IsWindowVisible", [w.HWND], w.BOOL),
            (self.user32, "IsIconic", [w.HWND], w.BOOL),
            (self.user32, "MonitorFromRect", [c.POINTER(w.RECT), w.DWORD], w.HANDLE),
            (self.user32, "GetMonitorInfoW", [w.HANDLE, c.POINTER(_MONITORINFO)], w.BOOL),
            (self.user32, "OpenInputDesktop", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.user32, "GetUserObjectInformationW",
             [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)], w.BOOL),
            (self.user32, "CloseDesktop", [w.HANDLE], w.BOOL),
            (self.user32, "GetDC", [w.HWND], w.HDC),
            (self.user32, "ReleaseDC", [w.HWND, w.HDC], c.c_int),
            (self.gdi32, "CreateCompatibleDC", [w.HDC], w.HDC),
            (self.gdi32, "CreateCompatibleBitmap", [w.HDC, c.c_int, c.c_int], w.HBITMAP),
            (self.gdi32, "SelectObject", [w.HDC, w.HANDLE], w.HANDLE),
            (self.gdi32, "SetStretchBltMode", [w.HDC, c.c_int], c.c_int),
            (self.gdi32, "SetBrushOrgEx", [w.HDC, c.c_int, c.c_int, c.POINTER(w.POINT)], w.BOOL),
            (self.gdi32, "StretchBlt", [w.HDC, c.c_int, c.c_int, c.c_int, c.c_int, w.HDC,
                                       c.c_int, c.c_int, c.c_int, c.c_int, w.DWORD], w.BOOL),
            (self.gdi32, "GetDIBits", [w.HDC, w.HBITMAP, w.UINT, w.UINT,
                                      c.c_void_p, c.c_void_p, w.UINT], c.c_int),
            (self.gdi32, "DeleteObject", [w.HANDLE], w.BOOL),
            (self.gdi32, "DeleteDC", [w.HDC], w.BOOL),
        ):
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, returns

    def _desktop_available(self) -> bool:
        desktop = self.user32.OpenInputDesktop(0, False, 0x0001)
        if not desktop:
            return False
        try:
            name = c.create_unicode_buffer(256)
            needed = w.DWORD()
            return bool(self.user32.GetUserObjectInformationW(
                desktop, 2, name, c.sizeof(name), c.byref(needed)
            )) and name.value.casefold() == "default"
        finally:
            self.user32.CloseDesktop(desktop)

    def foreground(self, scope: str) -> _CaptureTarget | None:
        if not self._desktop_available():
            return None
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd or not self.user32.IsWindowVisible(hwnd) or self.user32.IsIconic(hwnd):
            return None
        pid = w.DWORD()
        rect = w.RECT()
        if (not self.user32.GetWindowThreadProcessId(hwnd, c.byref(pid)) or not pid.value
                or not self.user32.GetWindowRect(hwnd, c.byref(rect))):
            return None
        monitor = self.user32.MonitorFromRect(c.byref(rect), 2)
        info = _MONITORINFO(cbSize=c.sizeof(_MONITORINFO))
        if not monitor or not self.user32.GetMonitorInfoW(monitor, c.byref(info)):
            return None
        full = info.rcMonitor
        if scope == "active_screen":
            bounds = (full.left, full.top, full.right, full.bottom)
        else:
            # A spanning window is limited to its foreground physical monitor.
            bounds = (max(rect.left, full.left), max(rect.top, full.top),
                      min(rect.right, full.right), min(rect.bottom, full.bottom))
        if bounds[2] - bounds[0] < 16 or bounds[3] - bounds[1] < 16:
            return None
        title = c.create_unicode_buffer(512)
        self.user32.GetWindowTextW(hwnd, title, len(title))
        # A browser tab can change with the same HWND/PID. Keep only a digest,
        # never the title, and include bounds so moving monitors invalidates it.
        digest = hashlib.sha256((title.value + repr((bounds, scope))).encode("utf-8")).hexdigest()[:32]
        token = f"{pid.value}:{int(hwnd)}:{digest}"
        return _CaptureTarget(int(hwnd), int(pid.value), bounds, token)

    def capture(self, target: _CaptureTarget, max_edge: int) -> tuple[bytes, int, int]:
        left, top, right, bottom = target.bounds
        original_width, original_height = right - left, bottom - top
        scale = min(1.0, max_edge / max(original_width, original_height))
        width, height = max(1, round(original_width * scale)), max(1, round(original_height * scale))
        source = self.user32.GetDC(None)
        output = bitmap = previous = None
        try:
            if not source:
                raise OSError("Visible desktop capture is unavailable")
            output = self.gdi32.CreateCompatibleDC(source)
            bitmap = self.gdi32.CreateCompatibleBitmap(source, width, height) if output else None
            if not output or not bitmap:
                raise OSError("Bounded capture allocation failed")
            previous = self.gdi32.SelectObject(output, bitmap)
            if not previous or previous == c.c_void_p(-1).value:
                raise OSError("Bounded capture bitmap is unavailable")
            self.gdi32.SetStretchBltMode(output, 4)  # HALFTONE
            self.gdi32.SetBrushOrgEx(output, 0, 0, None)
            if not self.gdi32.StretchBlt(output, 0, 0, width, height, source, left, top,
                                        original_width, original_height, 0x00CC0020 | 0x40000000):
                raise OSError("Visible desktop pixels are unavailable")
            self.gdi32.SelectObject(output, previous)
            info = _BITMAPINFOHEADER(biSize=c.sizeof(_BITMAPINFOHEADER), biWidth=width, biHeight=-height,
                                     biPlanes=1, biBitCount=32, biCompression=0)
            buffer = c.create_string_buffer(width * height * 4)
            if self.gdi32.GetDIBits(source, bitmap, 0, height, buffer, c.byref(info), 0) != height:
                raise OSError("Visible desktop pixel transfer failed")
            return _png_from_bgra(buffer.raw, width, height), width, height
        finally:
            if output and previous and previous != c.c_void_p(-1).value:
                self.gdi32.SelectObject(output, previous)
            if bitmap:
                self.gdi32.DeleteObject(bitmap)
            if output:
                self.gdi32.DeleteDC(output)
            if source:
                self.user32.ReleaseDC(None, source)


class ScreenCapture:
    """A caller-paced capture source; owns no background thread or disk image.

    The shell owns busy/fullscreen/input eligibility. This class independently
    rejects its own process, secure desktops, changed foregrounds and late work.
    is_current() reads only foreground metadata, never another screenshot.
    """

    def __init__(self, *, max_edge: int = 1440, excluded_pids: tuple[int, ...] = (),
                 backend: Any = None, clock: Callable[[], float] = time.monotonic) -> None:
        if type(max_edge) is not int or not 320 <= max_edge <= 1600:
            raise ValueError("max_edge must be an integer from 320 to 1600")
        self.max_edge = max_edge
        self._clock = clock
        self._excluded_pids = frozenset((os.getpid(), *excluded_pids))
        self._closed = threading.Event()
        self._capture_lock = threading.Lock()
        self._backend = backend
        self.last_status = "unavailable"
        if backend is None and sys.platform == "win32":
            try:
                self._backend = _Win32Capture()
            except (OSError, AttributeError):
                pass

    @property
    def available(self) -> bool:
        return self._backend is not None and not self._closed.is_set()

    def _live(self, cancel_event: threading.Event | None, expires_at: float | None) -> bool:
        return (not self._closed.is_set() and not (cancel_event is not None and cancel_event.is_set())
                and (expires_at is None or self._clock() < expires_at))

    def capture(self, *, scope: str = "active_window", cancel_event: threading.Event | None = None,
                expires_at: float | None = None) -> ScreenObservation | None:
        if scope not in SCREEN_SCOPES:
            raise ValueError("Unknown screen observation scope")
        if not self.available or not self._live(cancel_event, expires_at):
            self.last_status = "closed_or_unavailable"
            return None
        if not self._capture_lock.acquire(blocking=False):
            self.last_status = "busy"
            return None
        try:
            target = self._backend.foreground(scope)
            if target is None or target.pid in self._excluded_pids:
                self.last_status = "foreground_unavailable_or_owned"
                return None
            observed_at = self._clock()
            if not self._live(cancel_event, expires_at):
                return None
            png, width, height = self._backend.capture(target, self.max_edge)
            current = self._backend.foreground(scope)
            if (not self._live(cancel_event, expires_at) or current is None
                    or current.token != target.token or current.pid in self._excluded_pids):
                self.last_status = "cancelled_expired_or_changed"
                return None
            observation = ScreenObservation(
                png_bytes=png, image_id="screen-" + uuid.uuid4().hex, observed_at=observed_at,
                source=self._backend.source, scope=scope, active_window_token=target.token,
                width=width, height=height,
            )
            self.last_status = "captured"
            return observation
        except (OSError, ValueError):
            self.last_status = "capture_unavailable"
            return None
        finally:
            self._capture_lock.release()

    def is_current(self, observation: ScreenObservation, *, max_age_seconds: float = 60.0) -> bool:
        if not self.available or not isinstance(observation, ScreenObservation):
            return False
        age = self._clock() - observation.observed_at
        if not 0 <= age < max_age_seconds:
            return False
        try:
            target = self._backend.foreground(observation.scope)
            return bool(target is not None and target.pid not in self._excluded_pids
                        and target.token == observation.active_window_token and not self._closed.is_set())
        except OSError:
            return False

    def close(self) -> None:
        self._closed.set()
        self.last_status = "closed"
