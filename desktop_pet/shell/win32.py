"""Small, typed Win32 bindings; no pywin32, Pillow, or Qt dependency.

Original implementation against Microsoft API documentation. VPet's documented
layered-window/tray recovery pattern informed the design; no repository code or
character assets are copied. Research and licenses: outputs/pet-shell-v1/sources.json.
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import hashlib
from pathlib import Path
import struct
import zlib


user32 = c.WinDLL("user32", use_last_error=True)
shell32 = c.WinDLL("shell32", use_last_error=True)
kernel32 = c.WinDLL("kernel32", use_last_error=True)
gdi32 = c.WinDLL("gdi32", use_last_error=True)
LRESULT = c.c_ssize_t
WNDPROC = c.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
LONG_PTR = c.c_ssize_t
GetWindowLong = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
SetWindowLong = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
GetWindowLong.argtypes, GetWindowLong.restype = [w.HWND, c.c_int], LONG_PTR
SetWindowLong.argtypes, SetWindowLong.restype = [w.HWND, c.c_int, LONG_PTR], LONG_PTR
user32.CallWindowProcW.argtypes = [c.c_void_p, w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.CallWindowProcW.restype = LRESULT
user32.GetAncestor.argtypes, user32.GetAncestor.restype = [w.HWND, w.UINT], w.HWND
user32.SetWindowPos.argtypes = [w.HWND, w.HWND, c.c_int, c.c_int, c.c_int, c.c_int, w.UINT]
user32.SetWindowPos.restype = w.BOOL
user32.GetWindowRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
user32.MonitorFromRect.argtypes, user32.MonitorFromRect.restype = [c.POINTER(w.RECT), w.DWORD], w.HANDLE
user32.GetMonitorInfoW.argtypes = [w.HANDLE, c.c_void_p]
user32.RegisterHotKey.argtypes = [w.HWND, c.c_int, w.UINT, w.UINT]
user32.UnregisterHotKey.argtypes = [w.HWND, c.c_int]
user32.RegisterWindowMessageW.argtypes, user32.RegisterWindowMessageW.restype = [w.LPCWSTR], w.UINT
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.LoadImageW.argtypes, user32.LoadImageW.restype = [w.HINSTANCE, w.LPCWSTR, w.UINT, c.c_int, c.c_int, w.UINT], w.HANDLE
user32.DestroyIcon.argtypes = [w.HICON]
kernel32.CreateMutexW.argtypes, kernel32.CreateMutexW.restype = [c.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE
kernel32.CloseHandle.argtypes = [w.HANDLE]


class MONITORINFO(c.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


class GUID(c.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", c.c_ubyte * 8)]


class NOTIFYICONDATA(c.Structure):
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", w.HICON)]


shell32.Shell_NotifyIconW.argtypes = [w.DWORD, c.POINTER(NOTIFYICONDATA)]
shell32.Shell_NotifyIconW.restype = w.BOOL


def enable_dpi_awareness():
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [c.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = w.BOOL
        if user32.SetProcessDpiAwarenessContext(c.c_void_p(-4)):
            return "per-monitor-v2"
    except AttributeError:
        pass
    # ERROR_ACCESS_DENIED means an embedding host already chose its context.
    try:
        shcore = c.WinDLL("shcore")
        shcore.SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        user32.SetProcessDPIAware()
    return "host-or-per-monitor"


def hwnd_of(window) -> int:
    window.update_idletasks()
    return int(user32.GetAncestor(window.winfo_id(), 2) or window.winfo_id())


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = w.RECT()
    if not user32.GetWindowRect(hwnd, c.byref(rect)):
        raise c.WinError(c.get_last_error())
    return rect.left, rect.top, rect.right, rect.bottom


def cursor_position() -> tuple[int, int]:
    point = w.POINT()
    user32.GetCursorPos(c.byref(point))
    return point.x, point.y


def work_area(x: int, y: int, width: int = 1, height: int = 1) -> tuple[int, int, int, int]:
    rect = w.RECT(x, y, x + width, y + height)
    monitor = user32.MonitorFromRect(c.byref(rect), 2)
    info = MONITORINFO(cbSize=c.sizeof(MONITORINFO))
    if not user32.GetMonitorInfoW(monitor, c.byref(info)):
        raise c.WinError(c.get_last_error())
    return info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom


def monitor_layout() -> tuple[dict, ...]:
    """Actual monitor bounds and work areas in the host's physical DPI context.

    The host enables PerMonitorV2 before Tk. Never apply another per-monitor
    scale to these signed virtual-desktop coordinates.
    """
    callback_type = c.WINFUNCTYPE(w.BOOL, w.HANDLE, w.HDC, c.POINTER(w.RECT), w.LPARAM)
    user32.EnumDisplayMonitors.argtypes = [w.HDC, c.POINTER(w.RECT), callback_type, w.LPARAM]
    user32.EnumDisplayMonitors.restype = w.BOOL
    monitors = []

    def collect(handle, dc, rect, data):
        info = MONITORINFO(cbSize=c.sizeof(MONITORINFO))
        if user32.GetMonitorInfoW(handle, c.byref(info)):
            def coordinates(value):
                return (value.left, value.top, value.right, value.bottom)
            monitors.append({"bounds": coordinates(info.rcMonitor), "work_area": coordinates(info.rcWork),
                             "primary": bool(info.dwFlags & 1)})
        return True

    callback = callback_type(collect)
    if not user32.EnumDisplayMonitors(None, None, callback, 0) or not monitors:
        raise OSError("Windows did not return a visible monitor layout.")
    return tuple(monitors)


def clamp_position(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    left, top, right, bottom = work_area(x, y, width, height)
    return max(left, min(x, right - width)), max(top, min(y, bottom - height))


def move_window(hwnd: int, x: int, y: int, width: int, height: int, *, topmost: bool | None = None):
    # Win32 coordinates are absolute; Tk's negative geometry offsets are relative
    # to a screen edge and cannot represent a monitor left of the primary reliably.
    flags = 0x0010 | (0x0004 if topmost is None else 0)  # NOACTIVATE | NOZORDER
    insert_after = w.HWND(-1 if topmost else -2)
    if not user32.SetWindowPos(hwnd, insert_after, x, y, width, height, flags):
        raise c.WinError(c.get_last_error())


def passive_window(window):
    hwnd = hwnd_of(window)
    SetWindowLong(hwnd, -20, GetWindowLong(hwnd, -20) | 0x80 | 0x08000000)
    return hwnd


class InstanceGuard:
    def __init__(self, root_path: Path):
        suffix = hashlib.sha256(str(root_path.resolve()).lower().encode()).hexdigest()[:16]
        self.message = user32.RegisterWindowMessageW(f"NinoPixelPet.Recover.{suffix}")
        self.handle = kernel32.CreateMutexW(None, False, f"Local\\NinoPixelPet.{suffix}")
        if not self.handle:
            raise c.WinError(c.get_last_error())
        self.already_running = c.get_last_error() == 183
        if self.already_running:
            user32.PostMessageW(w.HWND(0xFFFF), self.message, 0, 0)

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None


class NativeShell:
    tray_message = 0x8000 + 37
    hotkey_id = 0x4E49

    def __init__(self, window, icon_path: Path, emit, recovery_message: int):
        self.hwnd = hwnd_of(window)
        self.emit = emit
        self.recovery_message = recovery_message
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._callback = WNDPROC(self._wndproc)
        self._original_proc = SetWindowLong(self.hwnd, -4, c.cast(self._callback, c.c_void_p).value)
        if not self._original_proc:
            raise c.WinError(c.get_last_error())
        style = GetWindowLong(self.hwnd, -20)
        SetWindowLong(self.hwnd, -20, (style | 0x80 | 0x08000000) & ~0x40000)  # TOOLWINDOW | NOACTIVATE
        self.icon = user32.LoadImageW(None, str(icon_path), 1, 32, 32, 0x10)
        self.tray = NOTIFYICONDATA(cbSize=c.sizeof(NOTIFYICONDATA), hWnd=self.hwnd, uID=1,
                                   uFlags=1 | 2 | 4, uCallbackMessage=self.tray_message, hIcon=self.icon)
        self.tray.szTip = "二乃 · 按兩下聊天｜右鍵選單"
        self.tray_available = bool(self.icon and shell32.Shell_NotifyIconW(0, c.byref(self.tray)))
        self.hotkey_available = bool(user32.RegisterHotKey(self.hwnd, self.hotkey_id, 0x4003, 0x4E))
        self.click_through = False

    def _wndproc(self, hwnd, message, wparam, lparam):
        if message == self.tray_message:
            event = lparam & 0xFFFF
            if event in (0x0203, 0x0202):  # double-click or button-up
                self.emit({"type": "native", "action": "open_chat"})
            elif event in (0x0205, 0x007B):
                self.emit({"type": "native", "action": "menu", "position": cursor_position()})
            return 0
        if message == 0x0312 and wparam == self.hotkey_id or message == self.recovery_message:
            self.emit({"type": "native", "action": "recover"})
            return 0
        if message == self.taskbar_created:
            self.emit({"type": "native", "action": "restore_tray"})
        return user32.CallWindowProcW(c.c_void_p(self._original_proc), hwnd, message, wparam, lparam)

    def set_click_through(self, enabled: bool):
        if enabled and not (self.tray_available or self.hotkey_available):
            raise RuntimeError("The tray and recovery hotkey are unavailable; click-through stays off.")
        style = GetWindowLong(self.hwnd, -20)
        SetWindowLong(self.hwnd, -20, style | 0x20 if enabled else style & ~0x20)
        self.click_through = enabled

    def restore_tray(self):
        self.tray_available = bool(shell32.Shell_NotifyIconW(0, c.byref(self.tray)))

    def set_icon(self, path: Path):
        icon = user32.LoadImageW(None, str(path), 1, 32, 32, 0x10)
        if icon:
            old = self.icon
            self.icon = self.tray.hIcon = icon
            shell32.Shell_NotifyIconW(1, c.byref(self.tray))
            if old:
                user32.DestroyIcon(old)

    def close(self):
        shell32.Shell_NotifyIconW(2, c.byref(self.tray))
        user32.UnregisterHotKey(self.hwnd, self.hotkey_id)
        SetWindowLong(self.hwnd, -4, self._original_proc)
        if self.icon:
            user32.DestroyIcon(self.icon)
            self.icon = None


class BITMAPINFOHEADER(c.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD),
                ("biBitCount", w.WORD), ("biCompression", w.DWORD), ("biSizeImage", w.DWORD),
                ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
                ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]


def capture_png(path: Path, rect: tuple[int, int, int, int]):
    """Capture the actual composited desktop region for visual acceptance."""
    left, top, right, bottom = rect
    width, height = max(1, right - left), max(1, bottom - top)
    user32.GetDC.argtypes, user32.GetDC.restype = [w.HWND], w.HDC
    user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
    gdi32.CreateCompatibleDC.argtypes, gdi32.CreateCompatibleDC.restype = [w.HDC], w.HDC
    gdi32.CreateCompatibleBitmap.argtypes, gdi32.CreateCompatibleBitmap.restype = [w.HDC, c.c_int, c.c_int], w.HBITMAP
    gdi32.SelectObject.argtypes, gdi32.SelectObject.restype = [w.HDC, w.HANDLE], w.HANDLE
    gdi32.BitBlt.argtypes = [w.HDC, c.c_int, c.c_int, c.c_int, c.c_int, w.HDC, c.c_int, c.c_int, w.DWORD]
    gdi32.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, c.c_void_p, c.c_void_p, w.UINT]
    gdi32.DeleteObject.argtypes = [w.HANDLE]
    gdi32.DeleteDC.argtypes = [w.HDC]
    source = user32.GetDC(None)
    target = gdi32.CreateCompatibleDC(source)
    bitmap = gdi32.CreateCompatibleBitmap(source, width, height)
    previous = gdi32.SelectObject(target, bitmap)
    try:
        if not gdi32.BitBlt(target, 0, 0, width, height, source, left, top, 0x00CC0020 | 0x40000000):
            raise c.WinError(c.get_last_error())
        gdi32.SelectObject(target, previous)
        info = BITMAPINFOHEADER(biSize=c.sizeof(BITMAPINFOHEADER), biWidth=width, biHeight=-height,
                                 biPlanes=1, biBitCount=32, biCompression=0)
        buffer = c.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(source, bitmap, 0, height, buffer, c.byref(info), 0):
            raise c.WinError(c.get_last_error())
        pixels = buffer.raw
        rgb = bytearray(width * height * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = pixels[2::4], pixels[1::4], pixels[0::4]
        scanlines = b"".join(b"\0" + rgb[row * width * 3:(row + 1) * width * 3] for row in range(height))
        def chunk(kind: bytes, value: bytes) -> bytes:
            return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png + chunk(b"IDAT", zlib.compress(scanlines, 6)) + chunk(b"IEND", b""))
    finally:
        gdi32.SelectObject(target, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(target)
        user32.ReleaseDC(None, source)
