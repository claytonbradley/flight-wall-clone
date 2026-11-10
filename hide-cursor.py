#!/usr/bin/env python3

"""Keep the X11 cursor hidden for the lifetime of this process."""

import ctypes
import ctypes.util
import sys
import time


RECONNECT_DELAY_SECONDS = 2
WINDOW_SCAN_INTERVAL_SECONDS = 0.25
X_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


@X_ERROR_HANDLER
def ignore_transient_x_error(_display, _error_event):
    """Ignore windows that disappear while the current tree is being scanned."""
    return 0


def load_library(name):
    library_path = ctypes.util.find_library(name)
    if not library_path:
        raise RuntimeError(f"required shared library is unavailable: {name}")
    return ctypes.CDLL(library_path)


def configure_x11_functions(x11, xfixes):
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
    x11.XDefaultScreen.restype = ctypes.c_int
    x11.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XRootWindow.restype = ctypes.c_ulong
    x11.XQueryTree.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
        ctypes.POINTER(ctypes.c_uint),
    ]
    x11.XQueryTree.restype = ctypes.c_int
    x11.XFree.argtypes = [ctypes.c_void_p]
    x11.XFree.restype = ctypes.c_int
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XSync.restype = ctypes.c_int
    x11.XSetErrorHandler.argtypes = [X_ERROR_HANDLER]
    x11.XSetErrorHandler.restype = ctypes.c_void_p
    xfixes.XFixesHideCursor.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    xfixes.XFixesHideCursor.restype = None


def child_windows(x11, display, window):
    root = ctypes.c_ulong()
    parent = ctypes.c_ulong()
    children = ctypes.POINTER(ctypes.c_ulong)()
    child_count = ctypes.c_uint()
    success = x11.XQueryTree(
        display,
        window,
        ctypes.byref(root),
        ctypes.byref(parent),
        ctypes.byref(children),
        ctypes.byref(child_count),
    )
    if not success:
        return []
    try:
        return [children[index] for index in range(child_count.value)]
    finally:
        if children:
            x11.XFree(children)


def hide_cursor_on_window_tree(x11, xfixes, display, root_window, hidden_windows):
    live_windows = set()
    pending_windows = [root_window]
    while pending_windows:
        window = pending_windows.pop()
        if window in live_windows:
            continue
        live_windows.add(window)
        # XFixes hide state can be undone by another X client. Reassert it for
        # every live window on each scan so the pointer cannot remain exposed.
        xfixes.XFixesHideCursor(display, window)
        pending_windows.extend(child_windows(x11, display, window))

    x11.XSync(display, False)
    hidden_windows.intersection_update(live_windows)
    hidden_windows.update(live_windows)


def connect_and_hide_cursor():
    x11 = load_library("X11")
    xfixes = load_library("Xfixes")
    configure_x11_functions(x11, xfixes)
    x11.XSetErrorHandler(ignore_transient_x_error)

    display = x11.XOpenDisplay(None)
    if not display:
        raise RuntimeError("X display is not ready")

    screen = x11.XDefaultScreen(display)
    root_window = x11.XRootWindow(display, screen)
    hidden_windows = set()
    while True:
        hide_cursor_on_window_tree(x11, xfixes, display, root_window, hidden_windows)
        time.sleep(WINDOW_SCAN_INTERVAL_SECONDS)


def main():
    while True:
        try:
            connect_and_hide_cursor()
        except RuntimeError as error:
            print(f"flightwall-hide-cursor: {error}; retrying", file=sys.stderr)
            time.sleep(RECONNECT_DELAY_SECONDS)


if __name__ == "__main__":
    main()
