#!/usr/bin/env python3

"""Keep the X11 cursor hidden for the lifetime of this process."""

import ctypes
import ctypes.util
import signal
import sys
import time


RECONNECT_DELAY_SECONDS = 2


def load_library(name):
    library_path = ctypes.util.find_library(name)
    if not library_path:
        raise RuntimeError(f"required shared library is unavailable: {name}")
    return ctypes.CDLL(library_path)


def connect_and_hide_cursor():
    x11 = load_library("X11")
    xfixes = load_library("Xfixes")

    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
    x11.XDefaultScreen.restype = ctypes.c_int
    x11.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XRootWindow.restype = ctypes.c_ulong
    x11.XFlush.argtypes = [ctypes.c_void_p]
    xfixes.XFixesHideCursor.argtypes = [ctypes.c_void_p, ctypes.c_ulong]

    display = x11.XOpenDisplay(None)
    if not display:
        raise RuntimeError("X display is not ready")

    screen = x11.XDefaultScreen(display)
    root_window = x11.XRootWindow(display, screen)
    xfixes.XFixesHideCursor(display, root_window)
    x11.XFlush(display)

    while True:
        signal.pause()


def main():
    while True:
        try:
            connect_and_hide_cursor()
        except RuntimeError as error:
            print(f"flightwall-hide-cursor: {error}; retrying", file=sys.stderr)
            time.sleep(RECONNECT_DELAY_SECONDS)


if __name__ == "__main__":
    main()
