"""Per-window Windows shell identity and persistent native icon ownership.

No Explorer restart, icon-cache deletion, shortcut editing, or other-app changes.
"""
import ctypes
import logging
import subprocess
import sys
import uuid
from pathlib import Path
from PySide6.QtCore import QObject, QEvent, QTimer

APP_ID = 'Siriy.StereoMotionMeasurement'
_ICONS = {}  # HICONs must outlive every Qt window and every shell refresh.


class GUID(ctypes.Structure):
    _fields_ = [('a', ctypes.c_uint32), ('b', ctypes.c_uint16),
                ('c', ctypes.c_uint16), ('d', ctypes.c_ubyte * 8)]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [('fmtid', GUID), ('pid', ctypes.c_uint32)]


class _Value(ctypes.Union):
    _fields_ = [('text', ctypes.c_void_p), ('padding', ctypes.c_ubyte * 16)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [('vt', ctypes.c_ushort), ('r1', ctypes.c_ushort),
                ('r2', ctypes.c_ushort), ('r3', ctypes.c_ushort), ('value', _Value)]


def window_properties(hwnd, values=None):
    """Use the window IPropertyStore, not only the process AppUserModelID."""
    shell = ctypes.windll.shell32
    props = ctypes.windll.propsys
    ole = ctypes.windll.ole32
    iid = GUID.from_buffer_copy(uuid.UUID('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99').bytes_le)
    pointer = ctypes.c_void_p()
    shell.SHGetPropertyStoreForWindow.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    result = shell.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(iid), ctypes.byref(pointer))
    if result != 0:
        raise OSError('SHGetPropertyStoreForWindow failed: %s' % result)
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    method = lambda index, *args: ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *args)(table[index])
    get = method(5, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))
    put = method(6, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))
    props.PSGetPropertyKeyFromName.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(PROPERTYKEY)]
    ole.PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]
    names = ('ID', 'RelaunchCommand', 'RelaunchIconResource', 'RelaunchDisplayNameResource')
    output = {}
    try:
        for name in names:
            key = PROPERTYKEY()
            result = props.PSGetPropertyKeyFromName('System.AppUserModel.' + name, ctypes.byref(key))
            if result != 0:
                raise OSError('Property key lookup failed: %s' % result)
            if values is not None and name in values:
                buffer = ctypes.create_unicode_buffer(values[name])
                value = PROPVARIANT()
                value.vt = 31  # VT_LPWSTR; caller owns buffer for this SetValue call.
                value.value.text = ctypes.cast(buffer, ctypes.c_void_p).value
                if put(pointer, ctypes.byref(key), ctypes.byref(value)) != 0:
                    raise OSError('Window property update failed: ' + name)
            value = PROPVARIANT()
            if get(pointer, ctypes.byref(key), ctypes.byref(value)) == 0:
                try:
                    output[name] = ctypes.wstring_at(value.value.text) if value.vt == 31 and value.value.text else ''
                finally:
                    ole.PropVariantClear(ctypes.byref(value))
        if values is not None:
            method(7)(pointer)
    finally:
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])(pointer)
    return output


class TaskbarGuard(QObject):
    def __init__(self, window, root):
        super().__init__(window)
        self.window = window
        self.root = Path(root)
        self.hwnd = None
        self.repairs = 0
        self._busy = False
        self.error = None
        self._pending = False
        self._force_pending = False
        self.timer = QTimer(self)
        if sys.platform != 'win32':
            return
        self.user = ctypes.windll.user32
        self.user.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        self.user.SendMessageW.restype = ctypes.c_ssize_t
        self.user.SetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        self.user.SetClassLongPtrW.restype = ctypes.c_size_t
        self.user.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        self.user.LoadImageW.restype = ctypes.c_void_p
        self.user.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
        self.shell_restart = self.user.RegisterWindowMessageW('TaskbarCreated')
        window.installEventFilter(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(3000)
        self.schedule(force=True)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Show, QEvent.WindowActivate, QEvent.WindowStateChange,
                            QEvent.WinIdChange, QEvent.WindowIconChange):
            self.schedule()
        return False

    def native_message(self, message):
        if sys.platform == 'win32':
            from ctypes.wintypes import MSG
            msg = MSG.from_address(int(message))
            if msg.message == self.shell_restart:
                self.schedule(force=True)

    def schedule(self, force=False):
        if sys.platform != 'win32':
            return
        self._force_pending |= force
        if not self._pending:
            self._pending = True
            QTimer.singleShot(0, self._scheduled_refresh)

    def _scheduled_refresh(self):
        self._pending = False
        force, self._force_pending = self._force_pending, False
        self.refresh(force)

    def icon_path(self):
        return next((p for p in (self.root/'assets/app.ico', Path(__file__).resolve().parent/'assets/app.ico') if p.exists()), None)

    def handles(self):
        path = self.icon_path()
        if path is None:
            raise FileNotFoundError('No application icon resource')
        hwnd = int(self.window.winId())
        dpi = self.user.GetDpiForWindow(ctypes.c_void_p(hwnd)) if hasattr(self.user, 'GetDpiForWindow') else 96
        sizes = (max(16, round(32*dpi/96)), max(16, round(16*dpi/96)))
        output = []
        for size in sizes:
            key = (str(path), size)
            if key not in _ICONS:
                _ICONS[key] = self.user.LoadImageW(None, str(path), 1, size, size, 0x10)
            if not _ICONS[key]:
                raise OSError('Native icon loading failed')
            output.append(_ICONS[key])
        return output

    def refresh(self, force=False):
        if sys.platform != 'win32' or self._busy:
            return
        self._busy = True
        try:
            hwnd = int(self.window.winId())
            if hwnd != self.hwnd or force:
                exe = Path(sys.executable).resolve()
                command = [str(exe)] if getattr(sys, 'frozen', False) else [str(exe), str(Path(__file__).with_name('app.py'))]
                resource = str(exe) + ',0' if getattr(sys, 'frozen', False) else str(self.icon_path()) + ',0'
                values = dict(ID=APP_ID, RelaunchCommand=subprocess.list2cmdline(command),
                              RelaunchIconResource=resource, RelaunchDisplayNameResource='Stereo Motion Measurement')
                properties = window_properties(hwnd, values)
                if properties['ID'] != APP_ID or properties['RelaunchIconResource'] != resource:
                    raise OSError('Shell identity verification failed')
                self.hwnd = hwnd
            big, small = self.handles()
            missing = self.user.SendMessageW(hwnd, 0x7f, 1, 0) != big or self.user.SendMessageW(hwnd, 0x7f, 0, 0) != small
            if missing or force:
                self.user.SendMessageW(hwnd, 0x80, 1, big)
                self.user.SendMessageW(hwnd, 0x80, 0, small)
                self.user.SetClassLongPtrW(hwnd, -14, big)
                self.user.SetClassLongPtrW(hwnd, -34, small)
                self.repairs += 1
                logging.info('Taskbar icon applied: count=%d native_window=%s', self.repairs, hwnd)
            self.error = None
        except Exception as error:
            if str(error) != self.error:
                logging.warning('Taskbar icon repair: %s', error)
                self.error = str(error)
        finally:
            self._busy = False

    def snapshot(self):
        if sys.platform != 'win32':
            return {'supported': False}
        hwnd = int(self.window.winId())
        big, small = self.handles()
        return dict(supported=True, error=self.error, repairs=self.repairs,
                    big_icon_matches=self.user.SendMessageW(hwnd, 0x7f, 1, 0) == big,
                    small_icon_matches=self.user.SendMessageW(hwnd, 0x7f, 0, 0) == small,
                    properties=window_properties(hwnd))
