"""Finite-frequency Win32 observations, with no content capture or input hooks.

Only executable basenames are classified locally; even those names are omitted
from snapshots. A foreground rectangle covering its monitor is a conservative
fullscreen signal, not a claim that a game/video/presentation was recognized.
"""
from __future__ import annotations

from collections.abc import Callable
import ctypes as c
from ctypes import wintypes as w
import ntpath
import os
import sys
import time

from .config import ActivityConfig
from .models import ActivityCategory, ActivitySnapshot, ObservationSource, Rect


class _LASTINPUTINFO(c.Structure):
    _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]


class _MONITORINFO(c.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


class _Win32Probe:
    def __init__(self, config: ActivityConfig):
        self.config = config
        self.user32 = c.WinDLL("user32", use_last_error=True)
        self.kernel32 = c.WinDLL("kernel32", use_last_error=True)
        self._bind(self.user32, "GetLastInputInfo", [c.POINTER(_LASTINPUTINFO)], w.BOOL)
        self._bind(self.user32, "GetForegroundWindow", [], w.HWND)
        self._bind(self.user32, "GetWindowThreadProcessId", [w.HWND, c.POINTER(w.DWORD)], w.DWORD)
        self._bind(self.user32, "GetWindowRect", [w.HWND, c.POINTER(w.RECT)], w.BOOL)
        self._bind(self.user32, "GetClassNameW", [w.HWND, w.LPWSTR, c.c_int], c.c_int)
        self._bind(self.user32, "MonitorFromRect", [c.POINTER(w.RECT), w.DWORD], w.HANDLE)
        self._bind(self.user32, "GetMonitorInfoW", [w.HANDLE, c.POINTER(_MONITORINFO)], w.BOOL)
        self._bind(self.user32, "GetCursorPos", [c.POINTER(w.POINT)], w.BOOL)
        self._bind(self.user32, "OpenInputDesktop", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
        self._bind(self.user32, "GetUserObjectInformationW", [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)], w.BOOL)
        self._bind(self.user32, "CloseDesktop", [w.HANDLE], w.BOOL)
        self._bind(self.kernel32, "GetTickCount64", [], c.c_ulonglong)
        self._bind(self.kernel32, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
        self._bind(self.kernel32, "QueryFullProcessImageNameW", [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)], w.BOOL)
        self._bind(self.kernel32, "CloseHandle", [w.HANDLE], w.BOOL)
        self._classification = {name.lower(): ActivityCategory(category)
                                for category, names in config.process_categories.items() for name in names}
        self._process_cache: tuple[int, float, ActivityCategory] | None = None

    @staticmethod
    def _bind(library, name, arguments, returns):
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, returns

    def _idle_seconds(self, diagnostics: list[str]) -> float | None:
        info = _LASTINPUTINFO(cbSize=c.sizeof(_LASTINPUTINFO))
        if not self.user32.GetLastInputInfo(c.byref(info)):
            diagnostics.append("last_input_unavailable")
            return None
        # LASTINPUTINFO uses the low 32 bits of the system tick count. Modular
        # subtraction handles wrap; future/ambiguous timestamps fail quiet.
        elapsed = (int(self.kernel32.GetTickCount64()) - info.dwTime) & 0xFFFFFFFF
        if elapsed > 0x7FFFFFFF:
            diagnostics.append("last_input_timestamp_ambiguous")
            return None
        return elapsed / 1000

    def _desktop_available(self, diagnostics: list[str]) -> bool | None:
        desktop = self.user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not desktop:
            diagnostics.append("input_desktop_unavailable")
            return False
        try:
            name = c.create_unicode_buffer(256)
            needed = w.DWORD()
            if not self.user32.GetUserObjectInformationW(desktop, 2, name, c.sizeof(name), c.byref(needed)):
                diagnostics.append("input_desktop_unknown")
                return None
            # Lock/UAC secure desktops are unavailable or non-Default. This is
            # an availability proxy, not a WTS session-lock notification.
            return name.value.lower() == "default"
        finally:
            self.user32.CloseDesktop(desktop)

    def _category(self, hwnd: int, now: float, diagnostics: list[str]) -> ActivityCategory:
        pid = w.DWORD()
        if not self.user32.GetWindowThreadProcessId(hwnd, c.byref(pid)) or not pid.value:
            diagnostics.append("foreground_process_unavailable")
            return ActivityCategory.UNKNOWN
        cached = self._process_cache
        if cached and cached[0] == pid.value and now < cached[1]:
            return cached[2]
        category = ActivityCategory.UNKNOWN
        handle = self.kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if handle:
            try:
                buffer = c.create_unicode_buffer(32768)
                size = w.DWORD(len(buffer))
                if self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, c.byref(size)):
                    category = self._classification.get(ntpath.basename(buffer.value).lower(), ActivityCategory.OTHER)
                else:
                    diagnostics.append("process_category_unavailable")
            finally:
                self.kernel32.CloseHandle(handle)
        else:
            diagnostics.append("process_category_unavailable")
        self._process_cache = (pid.value, now + self.config.process_cache_seconds, category)
        return category

    def _monitor(self, rect: w.RECT) -> _MONITORINFO | None:
        monitor = self.user32.MonitorFromRect(c.byref(rect), 2)  # MONITOR_DEFAULTTONEAREST
        info = _MONITORINFO(cbSize=c.sizeof(_MONITORINFO))
        return info if monitor and self.user32.GetMonitorInfoW(monitor, c.byref(info)) else None

    def work_area_for_rect(self, rect: Rect) -> Rect | None:
        info = self._monitor(w.RECT(round(rect.left), round(rect.top), round(rect.right), round(rect.bottom)))
        if info is None:
            return None
        area = info.rcWork
        return Rect(area.left, area.top, area.right, area.bottom)

    def __call__(self, now: float) -> ActivitySnapshot:
        diagnostics: list[str] = []
        idle = self._idle_seconds(diagnostics)
        available = self._desktop_available(diagnostics)
        point = w.POINT()
        mouse = (point.x, point.y) if self.user32.GetCursorPos(c.byref(point)) else None
        hwnd = self.user32.GetForegroundWindow()
        foreground_pid = w.DWORD()
        if hwnd:
            self.user32.GetWindowThreadProcessId(hwnd, c.byref(foreground_pid))
        foreground_owned = foreground_pid.value == os.getpid()
        category, fullscreen, work_area = ActivityCategory.UNKNOWN, False, None
        if available is True and hwnd:
            category = self._category(hwnd, now, diagnostics)
            bounds = w.RECT()
            if self.user32.GetWindowRect(hwnd, c.byref(bounds)):
                monitor = self._monitor(bounds)
                if monitor:
                    area, full = monitor.rcWork, monitor.rcMonitor
                    work_area = Rect(area.left, area.top, area.right, area.bottom)
                    window_class = c.create_unicode_buffer(256)
                    self.user32.GetClassNameW(hwnd, window_class, len(window_class))
                    is_desktop = window_class.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")
                    fullscreen = (not is_desktop and bounds.left <= full.left + 2 and bounds.top <= full.top + 2
                                  and bounds.right >= full.right - 2 and bounds.bottom >= full.bottom - 2)
                else:
                    diagnostics.append("foreground_monitor_unavailable")
            else:
                diagnostics.append("foreground_bounds_unavailable")
        elif available is True:
            diagnostics.append("foreground_window_unavailable")
        return ActivitySnapshot(now, idle, category, fullscreen, available, work_area, mouse,
                                ObservationSource.WIN32, tuple(diagnostics), foreground_owned)


class Win32ActivitySampler:
    """Call poll() from an existing UI loop; this class starts no thread/timer.

    None means disabled or not due. On an unsupported platform a due poll emits
    an UNAVAILABLE snapshot, which cannot create proactive events. The optional
    probe hook is for injected deterministic tests; mark those snapshots INJECTED.
    DPI awareness belongs to the host and is never changed here.
    """

    def __init__(self, config: ActivityConfig | None = None, *,
                 probe: Callable[[float], ActivitySnapshot] | None = None):
        self.config = config or ActivityConfig()
        self._probe = probe
        self._last_poll: float | None = None
        self.latest: ActivitySnapshot | None = None
        self._unavailable_reason = "unsupported_platform"
        if self.config.enabled and probe is None and sys.platform == "win32":
            try:
                self._probe = _Win32Probe(self.config)
            except (OSError, AttributeError):
                self._unavailable_reason = "win32_observer_unavailable"

    @property
    def available(self) -> bool:
        return self.config.enabled and self._probe is not None

    def poll(self, now: float | None = None) -> ActivitySnapshot | None:
        if not self.config.enabled:
            return None
        now = time.monotonic() if now is None else now
        if self._last_poll is not None and 0 <= now - self._last_poll < self.config.sample_interval_seconds:
            return None
        self._last_poll = now
        if self._probe is None:
            snapshot = ActivitySnapshot(now, None, desktop_available=None, source=ObservationSource.UNAVAILABLE,
                                        diagnostics=(self._unavailable_reason,))
        else:
            try:
                snapshot = self._probe(now)
            except (OSError, ValueError):
                snapshot = ActivitySnapshot(now, None, desktop_available=None, source=ObservationSource.UNAVAILABLE,
                                            diagnostics=("activity_sample_unavailable",))
        self.latest = snapshot
        return snapshot

    def work_area_for_rect(self, rect: Rect) -> Rect | None:
        """Optional monitor query for the pet, rather than foreground-window monitor.

        The UI may keep using its existing shell.work_area() instead. This method
        is explicit and not automatically called on every movement frame.
        """
        if isinstance(self._probe, _Win32Probe) and self.config.enabled:
            return self._probe.work_area_for_rect(rect)
        return None
