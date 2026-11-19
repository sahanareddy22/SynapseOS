"""Hardware-free virtual keyboard interaction tests."""
import importlib
import sys
import types
import unittest
from unittest.mock import Mock, patch

from desktop_input import DesktopInputCoordinator


class Signal:
    def __init__(self):
        self.callbacks = []
    def connect(self, callback): self.callbacks.append(callback)
    def emit(self, *args):
        for callback in tuple(self.callbacks): callback(*args)


class FakeButton:
    def __init__(self, text):
        self._text = text
        self.clicked = Signal()
        self.enabled = True
    def setFocusPolicy(self, policy): self.focus_policy = policy
    def text(self): return self._text
    def setMinimumSize(self, *args): self.minimum_size = args
    def setStyleSheet(self, style): self.style = style
    def mousePressEvent(self, _event): pass
    def mouseReleaseEvent(self, event):
        if event.button() == 1:
            self.click()
    def isEnabled(self): return self.enabled
    def isEnabled(self): return self.enabled
    def click(self): self.clicked.emit(False)


class FakeLayout:
    def __init__(self, *_args): self.placements = []
    def setSpacing(self, *_args): pass
    def setContentsMargins(self, *_args): pass
    def setHorizontalSpacing(self, *_args): pass
    def setVerticalSpacing(self, *_args): pass
    def setColumnStretch(self, *_args): pass
    def setRowStretch(self, *_args): pass
    def addWidget(self, *args): self.placements.append(args)


class FakeWidget:
    def __init__(self): self.visible = False; self.closed = False
    def setWindowTitle(self, title): self.title = title
    def setWindowFlags(self, flags): self.flags = flags
    def windowFlags(self): return self.flags
    def windowFlags(self): return self.flags
    def setStyleSheet(self, style): self._style = style
    def styleSheet(self): return getattr(self, "_style", "")
    def resize(self, *_args): pass
    def frameGeometry(self): return (0, 0, 980, 360)
    def devicePixelRatioF(self): return 1.0
    def frameGeometry(self): return (0, 0, 980, 360)
    def devicePixelRatioF(self): return 1.0
    def show(self): self.visible = True
    def close(self): self.closed = True; self.visible = False


def load_keyboard():
    pyautogui = types.ModuleType("pyautogui")
    pyautogui.press = Mock()
    pyautogui.write = Mock()
    pyautogui.hotkey = Mock()
    pyautogui.click = Mock()
    qt_core = types.ModuleType("PyQt5.QtCore")
    qt_core.Qt = types.SimpleNamespace(
        Window=1, WindowStaysOnTopHint=2, WindowDoesNotAcceptFocus=4, NoFocus=8, RightButton=2,
    )
    qt_widgets = types.ModuleType("PyQt5.QtWidgets")
    qt_widgets.QApplication = type("QApplication", (), {})
    qt_widgets.QGridLayout = FakeLayout
    qt_widgets.QPushButton = FakeButton
    qt_widgets.QWidget = FakeWidget
    with patch.dict(sys.modules, {
        "pyautogui": pyautogui,
        "PyQt5": types.ModuleType("PyQt5"),
        "PyQt5.QtCore": qt_core,
        "PyQt5.QtWidgets": qt_widgets,
    }):
        sys.modules.pop("vkeyboard", None)
        module = importlib.import_module("vkeyboard")
    return module.VirtualKeyboard, pyautogui, qt_core.Qt


class VirtualKeyboardTests(unittest.TestCase):
    def setUp(self):
        self.Keyboard, self.desktop, self.qt = load_keyboard()
        self.status = Mock()
        self.keyboard = self.Keyboard(on_status=self.status)

    def test_keyboard_is_topmost_mouse_interactive_and_does_not_take_focus(self):
        expected = self.qt.Window | self.qt.WindowStaysOnTopHint | self.qt.WindowDoesNotAcceptFocus
        self.assertEqual(self.keyboard.flags, expected)
        self.assertEqual(self.keyboard.buttons["A"][0].focus_policy, self.qt.NoFocus)

    def test_keyboard_layout_fits_fifteen_columns_without_overlap_and_keeps_all_keys_visible(self):
        positions = self.keyboard.layout_positions
        self.assertTrue(positions)
        for row in range(6):
            row_positions = sorted((column, span) for item_row, column, span, _button in positions if item_row == row)
            cursor = 0
            for column, span in row_positions:
                self.assertEqual(column, cursor, "row {} contains an overlap or gap".format(row))
                cursor += span
            self.assertEqual(cursor, 15)
        expected = {"ESC", "F1", "F12", "PRTSC", "DELETE", "BACKSPACE", "TAB", "Q", "CAPSLOCK",
                    "ENTER", "SHIFT", "SPACE", "CTRL", "ALT", "WIN", "LEFT", "RIGHT", "UP", "DOWN"}
        self.assertTrue(expected.issubset(self.keyboard.buttons))
        self.assertGreaterEqual(self.keyboard.buttons["SPACE"][0].minimum_size[0],
                                self.keyboard.buttons["A"][0].minimum_size[0] * 4)

    def test_light_keyboard_theme_and_active_modifier_states_use_sage(self):
        self.assertIn("#F7F6F2", self.keyboard.styleSheet())
        self.assertNotIn("#131b27", self.keyboard.styleSheet().lower())
        self.keyboard.key_pressed("SHIFT")
        self.assertIn("#DDE5D8", self.keyboard.buttons["SHIFT"][0].style)
        self.keyboard.key_pressed("CAPSLOCK")
        self.assertIn("#DDE5D8", self.keyboard.buttons["CAPSLOCK"][0].style)

    def test_qt_mouse_press_and_release_are_logged_and_passed_to_button(self):
        button = self.keyboard.buttons["A"][0]
        event = types.SimpleNamespace(button=lambda: 1)
        with self.assertLogs("vkeyboard", level="INFO") as logs:
            button.mousePressEvent(event)
            button.mouseReleaseEvent(event)
        self.assertIn("mouse_press_received key=A", " ".join(logs.output))
        self.assertIn("mouse_release_received key=A", " ".join(logs.output))

    def test_right_eye_right_click_activates_only_the_virtual_keyboard_key(self):
        button = self.keyboard.buttons["A"][0]
        from tests.test_eye_control import eye_module, fake_pyautogui
        coordinator = DesktopInputCoordinator()
        controller = eye_module.EyeController(input_coordinator=coordinator)
        right_event = types.SimpleNamespace(button=lambda: self.qt.RightButton)
        fake_pyautogui.mouseDown.side_effect = lambda **_kwargs: button.mousePressEvent(right_event)
        fake_pyautogui.mouseUp.side_effect = lambda **_kwargs: button.mouseReleaseEvent(right_event)
        try:
            self.assertTrue(controller._dispatch_blink_action("right"))
        finally:
            fake_pyautogui.mouseDown.side_effect = None
            fake_pyautogui.mouseUp.side_effect = None
            controller.close()
        self.desktop.write.assert_called_once_with("a")

    def test_left_eye_left_click_activates_virtual_keyboard_key(self):
        button = self.keyboard.buttons["A"][0]
        from tests.test_eye_control import eye_module, fake_pyautogui
        coordinator = DesktopInputCoordinator()
        controller = eye_module.EyeController(input_coordinator=coordinator)
        left_event = types.SimpleNamespace(button=lambda: 1)
        fake_pyautogui.mouseDown.side_effect = lambda **_kwargs: button.mousePressEvent(left_event)
        fake_pyautogui.mouseUp.side_effect = lambda **_kwargs: button.mouseReleaseEvent(left_event)
        try:
            self.assertTrue(controller._dispatch_blink_action("left"))
        finally:
            fake_pyautogui.mouseDown.side_effect = None
            fake_pyautogui.mouseUp.side_effect = None
            controller.close()
        self.desktop.write.assert_called_once_with("a")

    def test_qt_button_click_enters_character_and_keeps_keyboard_open(self):
        coordinator = DesktopInputCoordinator()
        self.desktop.click.side_effect = lambda: self.keyboard.buttons["A"][0].click()

        accepted = coordinator.run_eye_action("click", lambda: self.desktop.click())

        self.assertTrue(accepted)
        self.desktop.click.assert_called_once_with()
        self.desktop.write.assert_called_once_with("a")
        self.assertFalse(self.keyboard.closed)

    def test_key_handler_logs_key_identifier_without_logging_text_payload(self):
        with self.assertLogs("vkeyboard", level="INFO") as logs:
            self.keyboard.key_pressed("A")
            self.keyboard.type_text("private example")
        self.assertIn("key_handler_called key=A", " ".join(logs.output))
        self.assertNotIn("private example", " ".join(logs.output))

    def test_input_adapter_receives_character_and_special_key_actions(self):
        adapter = Mock()
        keyboard = self.Keyboard(on_status=self.status, input_adapter=adapter)
        keyboard.key_pressed("A")
        keyboard.key_pressed("SHIFT")
        keyboard.key_pressed("B")
        keyboard.key_pressed("BACKSPACE")
        keyboard.key_pressed("SPACE")
        keyboard.key_pressed("ENTER")
        keyboard.key_pressed("CAPSLOCK")
        keyboard.key_pressed("C")
        self.assertEqual(adapter.call_args_list, [
            unittest.mock.call("write", "a"),
            unittest.mock.call("hotkey", "shift", "b"),
            unittest.mock.call("press", "backspace"),
            unittest.mock.call("press", "space"),
            unittest.mock.call("press", "enter"),
            unittest.mock.call("write", "C"),
        ])
        self.assertFalse(keyboard.closed)

    def test_shift_caps_lock_and_special_keys_activate_expected_outputs(self):
        self.keyboard.buttons["A"][0].click()
        self.assertEqual(self.desktop.write.call_args.args, ("a",))
        self.keyboard.buttons["SHIFT"][0].click()
        self.keyboard.buttons["Q"][0].click()
        self.desktop.hotkey.assert_called_once_with("shift", "q")

        self.keyboard.buttons["CAPSLOCK"][0].click()
        self.keyboard.buttons["B"][0].click()
        self.assertEqual(self.desktop.write.call_args.args, ("B",))
        self.keyboard.buttons["BACKSPACE"][0].click()
        self.keyboard.buttons["SPACE"][0].click()
        self.keyboard.buttons["ENTER"][0].click()
        self.assertEqual(self.desktop.press.call_args_list, [
            unittest.mock.call("backspace"),
            unittest.mock.call("space"),
            unittest.mock.call("enter"),
        ])

    def test_special_key_error_is_reported_without_hiding_keyboard(self):
        self.desktop.press.side_effect = RuntimeError("mock key failure")
        self.keyboard.buttons["ENTER"][0].click()
        self.status.assert_called_once()
        self.assertFalse(self.keyboard.closed)


if __name__ == "__main__":
    unittest.main()
