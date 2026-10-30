"""Press OptiScaler's menu key in the running game, from the Quick Access panel.

OptiScaler opens and closes its own overlay on a keyboard shortcut ([Menu]
ShortcutKey, Insert by default). A Deck has no keyboard, so this module makes a
virtual one through Linux uinput and presses that key. Gamescope picks the
device up like any USB keyboard and delivers the key to the focused window,
which is the game once the Quick Access menu has closed; Wine turns it into the
same WM_KEYDOWN/VK code a Windows keyboard would.

Only the standard library is used (fcntl + struct over /dev/uinput), so the
plugin gains no dependency. /dev/uinput is the node Steam Input itself uses for
its virtual controllers; SteamOS and Bazzite grant it to the logged-in user, so
the plugin's backend (which runs as that user) can open it without root.

The device is created on first use and kept for the life of the plugin: a
freshly created keyboard takes a moment to be noticed, and a press sent before
then is lost.
"""

import fcntl
import os
import struct
import threading
import time

UINPUT_PATH = "/dev/uinput"

# linux/input-event-codes.h
EV_SYN, EV_KEY = 0x00, 0x01
SYN_REPORT = 0
BUS_USB = 0x03

# linux/uinput.h ioctls, built with the _IOC macro so the sizes are explicit.
_IOC_NONE, _IOC_WRITE = 0, 1


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("U") << 8) | nr


UINPUT_SETUP_FORMAT = "HHHH80sI"  # struct input_id + name[80] + ff_effects_max
UI_DEV_CREATE = _ioc(_IOC_NONE, 1, 0)
UI_DEV_DESTROY = _ioc(_IOC_NONE, 2, 0)
UI_DEV_SETUP = _ioc(_IOC_WRITE, 3, struct.calcsize(UINPUT_SETUP_FORMAT))
UI_SET_EVBIT = _ioc(_IOC_WRITE, 100, struct.calcsize("i"))
UI_SET_KEYBIT = _ioc(_IOC_WRITE, 101, struct.calcsize("i"))

# struct input_event on 64-bit: struct timeval (two longs), type, code, value.
INPUT_EVENT_FORMAT = "llHHi"

# Windows virtual-key code (what OptiScaler stores) -> Linux evdev key code.
VK_TO_EVDEV = {
    0x08: 14,   # Backspace
    0x09: 15,   # Tab
    0x0D: 28,   # Enter
    0x13: 119,  # Pause
    0x14: 58,   # Caps Lock
    0x1B: 1,    # Escape
    0x20: 57,   # Space
    0x21: 104,  # Page Up
    0x22: 109,  # Page Down
    0x23: 107,  # End
    0x24: 102,  # Home
    0x25: 105,  # Left
    0x26: 103,  # Up
    0x27: 106,  # Right
    0x28: 108,  # Down
    0x2C: 99,   # Print Screen
    0x2D: 110,  # Insert
    0x2E: 111,  # Delete
    0x90: 69,   # Num Lock
    0x91: 70,   # Scroll Lock
    0xC0: 41,   # ` ~
}
# F1-F12
VK_TO_EVDEV.update({0x70 + i: code for i, code in enumerate(
    [59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 87, 88])})
# 0-9 (evdev runs 1..9 then 0)
VK_TO_EVDEV.update({0x30 + d: (11 if d == 0 else 1 + d) for d in range(10)})
# A-Z, in evdev's keyboard-row order
_LETTERS = {"Q": 16, "W": 17, "E": 18, "R": 19, "T": 20, "Y": 21, "U": 22, "I": 23, "O": 24,
            "P": 25, "A": 30, "S": 31, "D": 32, "F": 33, "G": 34, "H": 35, "J": 36, "K": 37,
            "L": 38, "Z": 44, "X": 45, "C": 46, "V": 47, "B": 48, "N": 49, "M": 50}
VK_TO_EVDEV.update({ord(letter): code for letter, code in _LETTERS.items()})

VK_INSERT = 0x2D
#: How long the key is held. OptiScaler may poll the key once per frame, so the
#: press has to outlast a frame even at a low frame rate.
HOLD_SECONDS = 0.12
#: How long a new device is given to be noticed before the first press.
SETTLE_SECONDS = 1.0

_lock = threading.Lock()
_fd = None


def parse_shortcut(value):
    """The VK code a [Menu] ShortcutKey value names; Insert for auto/unset/unreadable.

    OptiScaler writes it as hex ("0x2D") or decimal ("45").
    """
    text = str(value or "").strip().lower()
    if text in ("", "auto"):
        return VK_INSERT
    try:
        code = int(text, 0)
    except ValueError:
        return VK_INSERT
    return code if 0 < code < 0x100 else VK_INSERT


def _event(fd, ev_type, code, value):
    os.write(fd, struct.pack(INPUT_EVENT_FORMAT, 0, 0, ev_type, code, value))


def _device():
    """The virtual keyboard, created on first use."""
    global _fd
    if _fd is not None:
        return _fd, False
    fd = os.open(UINPUT_PATH, os.O_WRONLY | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        for code in sorted(set(VK_TO_EVDEV.values())):
            fcntl.ioctl(fd, UI_SET_KEYBIT, code)
        # Generic ids (as python-evdev's UInput uses): no registered vendor.
        setup = struct.pack(UINPUT_SETUP_FORMAT, BUS_USB, 0x0001, 0x0001, 1,
                            b"Decky OptiScaler menu key", 0)
        fcntl.ioctl(fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(fd, UI_DEV_CREATE)
    except Exception:
        os.close(fd)
        raise
    _fd = fd
    return fd, True


def press(vk):
    """Press and release one key on the virtual keyboard."""
    code = VK_TO_EVDEV.get(vk)
    if code is None:
        raise ValueError(f"OptiScaler's menu key (VK 0x{vk:02X}) has no keyboard mapping here")
    with _lock:
        try:
            fd, created = _device()
        except PermissionError as exc:
            raise PermissionError(
                f"No access to {UINPUT_PATH}, so the plugin cannot press the menu key"
            ) from exc
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"{UINPUT_PATH} does not exist (uinput not loaded)") from exc
        if created:
            time.sleep(SETTLE_SECONDS)
        _event(fd, EV_KEY, code, 1)
        _event(fd, EV_SYN, SYN_REPORT, 0)
        time.sleep(HOLD_SECONDS)
        _event(fd, EV_KEY, code, 0)
        _event(fd, EV_SYN, SYN_REPORT, 0)
    return {"vk": vk, "evdev": code}


def close():
    """Remove the virtual keyboard (plugin unload)."""
    global _fd
    with _lock:
        if _fd is None:
            return
        try:
            fcntl.ioctl(_fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(_fd)
        _fd = None
