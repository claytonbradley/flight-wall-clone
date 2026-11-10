import importlib.util
import pathlib
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("flightwall_hide_cursor", ROOT / "hide-cursor.py")
hide_cursor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hide_cursor)


class CursorWindowTreeTests(unittest.TestCase):
    def test_hides_existing_and_new_windows_without_incrementing_existing_hide_counts(self):
        window_tree = {
            1: [2, 3],
            2: [],
            3: [],
        }
        x11 = mock.Mock()
        xfixes = mock.Mock()
        hidden_windows = set()

        with mock.patch.object(
            hide_cursor,
            "child_windows",
            side_effect=lambda _x11, _display, window: window_tree[window],
        ):
            hide_cursor.hide_cursor_on_window_tree(
                x11, xfixes, "display", 1, hidden_windows
            )
            window_tree[3] = [4]
            window_tree[4] = []
            hide_cursor.hide_cursor_on_window_tree(
                x11, xfixes, "display", 1, hidden_windows
            )

        first_scan_windows = {
            call.args[1] for call in xfixes.XFixesHideCursor.call_args_list[:3]
        }
        self.assertEqual({1, 2, 3}, first_scan_windows)
        self.assertEqual(mock.call("display", 4), xfixes.XFixesHideCursor.call_args_list[3])
        self.assertEqual(4, xfixes.XFixesHideCursor.call_count)
        self.assertEqual({1, 2, 3, 4}, hidden_windows)
        self.assertEqual(2, x11.XSync.call_count)

    def test_forgets_destroyed_windows_so_reused_ids_are_hidden_again(self):
        live_children = [2]
        x11 = mock.Mock()
        xfixes = mock.Mock()
        hidden_windows = set()

        def children_for_window(_x11, _display, window):
            return live_children if window == 1 else []

        with mock.patch.object(hide_cursor, "child_windows", side_effect=children_for_window):
            hide_cursor.hide_cursor_on_window_tree(x11, xfixes, None, 1, hidden_windows)
            live_children.clear()
            hide_cursor.hide_cursor_on_window_tree(x11, xfixes, None, 1, hidden_windows)
            live_children.append(2)
            hide_cursor.hide_cursor_on_window_tree(x11, xfixes, None, 1, hidden_windows)

        hidden_window_ids = [call.args[1] for call in xfixes.XFixesHideCursor.call_args_list]
        self.assertEqual([1, 2, 2], hidden_window_ids)


if __name__ == "__main__":
    unittest.main()
