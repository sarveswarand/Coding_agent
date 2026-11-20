"""Keyboard input for the voice assistant (Windows only).

The assistant may type into a window only when that window belongs to a program on TYPE_TARGETS
(browsers and Notepad by default). Never a terminal or an editor, because typing plus Enter there
would run commands by voice without any approval. Change the list with VOICE_TYPE_APPS in .env
(comma-separated program names). Windows won't deliver synthetic keys to an elevated (admin) window.
"""
import ctypes
import os
from ctypes import wintypes

DEFAULT_TARGETS = "chrome.exe,msedge.exe,notepad.exe"
MAX_TYPED_CHARS = 300
KEYS = {"enter": 0x0D, "tab": 0x09, "escape": 0x1B, "backspace": 0x08}

INPUT_KEYBOARD, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 1, 0x0002, 0x0004
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def supported() -> bool:
    return os.name == "nt"


def type_targets() -> set[str]:
    raw = os.environ.get("VOICE_TYPE_APPS", DEFAULT_TARGETS)
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


if supported():
    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _MOUSEINPUT(ctypes.Structure):  # only here so the union has the size Windows expects
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    _user32.SendInput.restype = wintypes.UINT
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def foreground_program() -> str | None:
    """Program name (like 'chrome.exe') of the window in front, or None."""
    if not supported():
        return None
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), ctypes.byref(pid))
    if not pid.value:
        return None
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        buf, size = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return os.path.basename(buf.value).lower()
    finally:
        _kernel32.CloseHandle(handle)


def _send(events: list[tuple[int, int, int]]) -> None:
    """events are (virtual key, unicode scan code, flags)."""
    arr = (_INPUT * len(events))()
    for i, (vk, scan, flags) in enumerate(events):
        arr[i].type = INPUT_KEYBOARD
        arr[i].ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    _user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))


def type_text(text: str) -> None:
    """Type `text` into the window in front, as if on the keyboard (no Enter, newlines become spaces)."""
    events: list[tuple[int, int, int]] = []
    data = text.replace("\r", " ").replace("\n", " ")[:MAX_TYPED_CHARS].encode("utf-16-le")
    for i in range(0, len(data), 2):
        unit = int.from_bytes(data[i:i + 2], "little")
        events += [(0, unit, KEYEVENTF_UNICODE), (0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    if events:
        _send(events)


def press(key: str) -> None:
    vk = KEYS[key]
    _send([(vk, 0, 0), (vk, 0, KEYEVENTF_KEYUP)])


def screenshot_png(max_width: int = 1024) -> bytes:
    """The main screen as PNG bytes, scaled down so a small local model can read it in reasonable time.
    Kept in memory only."""
    import io

    from PIL import ImageGrab
    img = ImageGrab.grab()
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)))
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()


MODIFIERS = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10}
SPECIAL_KEYS = {"tab": 0x09, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "pageup": 0x21,
                "pagedown": 0x22, "home": 0x24, "end": 0x23, "f5": 0x74}


def hotkey(*keys: str) -> None:
    """Press a shortcut such as ("ctrl", "l") or ("ctrl", "shift", "tab"): any of ctrl/alt/shift, then one key
    from a short list (a letter, a digit, or tab/arrows/page keys/F5). Used by the assistant's own commands
    (the browser's address bar and tabs), never built from free-form speech."""
    *mods, key = keys
    if not all(m in MODIFIERS for m in mods) or not mods:
        raise ValueError("unsupported shortcut")
    if key in SPECIAL_KEYS:
        vk = SPECIAL_KEYS[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    else:
        raise ValueError("unsupported shortcut")
    down = [(MODIFIERS[m], 0, 0) for m in mods]
    up = [(MODIFIERS[m], 0, KEYEVENTF_KEYUP) for m in reversed(mods)]
    _send(down + [(vk, 0, 0), (vk, 0, KEYEVENTF_KEYUP)] + up)
