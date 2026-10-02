"""Windows Shell identity for the window and its installed shortcut."""
import ctypes as ct
import uuid

APP_ID = "ArenaOnAir.Broadcast"


class GUID(ct.Structure):
    _fields_ = [("data", ct.c_ubyte * 16)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class PROPERTYKEY(ct.Structure):
    _fields_ = [("fmtid", GUID), ("pid", ct.c_ulong)]


class VALUE(ct.Union):
    _fields_ = [("text", ct.c_wchar_p), ("padding", ct.c_ubyte * 16)]


class PROPVARIANT(ct.Structure):
    _fields_ = [("vt", ct.c_ushort), ("reserved", ct.c_ushort * 3), ("value", VALUE)]


def _check(result):
    if result < 0:
        raise OSError(f"Windows taskbar property failed: 0x{result & 0xffffffff:08x}")


def set_properties(*, hwnd=None, shortcut=None, values):
    """Set AppUserModel string properties through the Shell's IPropertyStore."""
    ole = ct.WinDLL("ole32")
    result = ole.CoInitializeEx(None, 2)
    # Qt may already have initialized COM with a different apartment model.
    if result != -2147417850:  # RPC_E_CHANGED_MODE
        _check(result)
    try:
        _set_properties(hwnd=hwnd, shortcut=shortcut, values=values)
    finally:
        if result >= 0:
            ole.CoUninitialize()


def _set_properties(*, hwnd, shortcut, values):
    shell = ct.WinDLL("shell32")
    iid = GUID.parse("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
    store = ct.c_void_p()
    if hwnd is not None:
        get_store = shell.SHGetPropertyStoreForWindow
        get_store.argtypes = [ct.c_void_p, ct.POINTER(GUID), ct.POINTER(ct.c_void_p)]
        _check(get_store(hwnd, ct.byref(iid), ct.byref(store)))
    else:
        get_store = shell.SHGetPropertyStoreFromParsingName
        get_store.argtypes = [ct.c_wchar_p, ct.c_void_p, ct.c_uint,
                             ct.POINTER(GUID), ct.POINTER(ct.c_void_p)]
        _check(get_store(str(shortcut), None, 2, ct.byref(iid), ct.byref(store)))
    table = ct.cast(store, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
    release = ct.WINFUNCTYPE(ct.c_ulong, ct.c_void_p)(table[2])
    set_value = ct.WINFUNCTYPE(ct.c_long, ct.c_void_p, ct.POINTER(PROPERTYKEY),
                             ct.POINTER(PROPVARIANT))(table[6])
    commit = ct.WINFUNCTYPE(ct.c_long, ct.c_void_p)(table[7])
    try:
        for pid, text in values.items():
            key = PROPERTYKEY(GUID.parse("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"), pid)
            value = PROPVARIANT()
            value.vt = 31  # VT_LPWSTR; Python owns the string for this call.
            value.value.text = text
            _check(set_value(store, ct.byref(key), ct.byref(value)))
        if shortcut is not None:
            _check(commit(store))
    finally:
        release(store)
