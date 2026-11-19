import sys
import logging

import pyautogui
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QGridLayout, QPushButton, QWidget


logger = logging.getLogger(__name__)


class DiagnosticKeyButton(QPushButton):
    """Standard QPushButton with non-interfering mouse event diagnostics."""

    def mousePressEvent(self, event):
        logger.info("VKEYBOARD mouse_press_received key=%s button=%s",
                    self.text(), event.button())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        logger.info("VKEYBOARD mouse_release_received key=%s button=%s",
                    self.text(), event.button())
        super().mouseReleaseEvent(event)
        # EyeController intentionally maps the right-eye blink to a right
        # mouse click. QPushButton does not emit clicked for a right click,
        # so translate that release to the same key activation locally.
        if event.button() == Qt.RightButton and self.isEnabled():
            self.click()


class VirtualKeyboard(QWidget):
    def __init__(self, on_status=None, on_select=None, input_adapter=None):
        super().__init__()
        self.setWindowTitle("SynapseOS Virtual Keyboard")
        # Keep the keyboard above other windows without stealing keyboard focus
        # from the application that should receive the simulated keystrokes.
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.on_status = on_status or (lambda _message: None)
        self.on_select = on_select or (lambda _key: None)
        self.input_adapter = input_adapter
        self.modifiers = set()
        self.buttons = {}
        self.setStyleSheet("""
            QWidget { background: #F7F6F2; color: #353630; font-family: 'Segoe UI'; }
            QPushButton { background: #FFFFFF; color: #353630; border: 1px solid #D8D6CE;
                          border-radius: 5px; font-size: 15px; font-weight: 600; padding: 5px; }
            QPushButton:hover { background: #F0F1EC; border-color: #AAB4A3; }
            QPushButton:pressed { background: #DDE5D8; border-color: #82927B; }
            QPushButton:disabled { color: #A5A49C; background: #EFEEE9; }
            QLabel { color: #77776F; }
        """)
        grid = QGridLayout(self)
        grid.setContentsMargins(18, 16, 18, 16)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        columns = 15
        for column in range(columns):
            grid.setColumnStretch(column, 1)
        for row_index in range(6):
            grid.setRowStretch(row_index, 1)
        rows = [
            [(key, 1) for key in ["ESC", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "PRTSC", "DELETE"]],
            [(key, 1) for key in ["`", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "="]] + [("BACKSPACE", 2)],
            [("TAB", 2)] + [(key, 1) for key in ["Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P", "[", "]", "\\"]],
            [("CAPSLOCK", 2)] + [(key, 1) for key in ["A", "S", "D", "F", "G", "H", "J", "K", "L", ";", "'"]] + [("ENTER", 2)],
            [("SHIFT", 2)] + [(key, 1) for key in ["Z", "X", "C", "V", "B", "N", "M", ",", ".", "/"]] + [("SHIFT", 3)],
            [(key, 1) for key in ["CTRL", "WIN", "ALT"]] + [("SPACE", 6)] + [(key, 1) for key in ["ALT", "CTRL", "LEFT", "UP", "DOWN", "RIGHT"]],
        ]
        self.caps_lock = False
        self.layout_positions = []
        base_width, base_height = self._responsive_size()
        key_width = max(34, int((base_width - 36 - (columns - 1) * 8) / columns))
        key_height = max(48, int((base_height - 72) / len(rows)))
        for row_index, row in enumerate(rows):
            column_index = 0
            for key, span in row:
                button = DiagnosticKeyButton(key)
                button.setFocusPolicy(Qt.NoFocus)
                button.setMinimumSize(key_width * span, key_height)
                button.clicked.connect(lambda _checked=False, selected=key: self.key_pressed(selected))
                grid.addWidget(button, row_index, column_index, 1, span)
                self.layout_positions.append((row_index, column_index, span, button))
                column_index += span
                self.buttons.setdefault(key, []).append(button)
        self.resize(base_width, base_height)
        logger.info("VKEYBOARD initialized flags=%s buttons=%d", self.windowFlags(),
                    sum(len(buttons) for buttons in self.buttons.values()))

    @staticmethod
    def _responsive_size():
        """Choose a large, screen-aware size in Qt's device-independent pixels."""
        try:
            screen = QApplication.primaryScreen()
            geometry = screen.availableGeometry() if screen is not None else None
            if geometry is not None:
                width = geometry.width()
                height = geometry.height()
                return (max(min(980, width), min(1320, int(width * 0.92))),
                        max(min(430, height), min(560, int(height * 0.56))))
        except (AttributeError, RuntimeError, TypeError):
            pass
        return 1120, 500

    def key_pressed(self, key):
        logger.info("VKEYBOARD key_handler_called key=%s", key)
        modifier_names = {"SHIFT": "shift", "CTRL": "ctrl", "ALT": "alt", "WIN": "win"}
        if key in modifier_names:
            modifier = modifier_names[key]
            if modifier in self.modifiers:
                self.modifiers.remove(modifier)
            else:
                self.modifiers.add(modifier)
            self._refresh_modifiers()
            return
        if key == "CAPSLOCK":
            self.caps_lock = not self.caps_lock
            self.buttons[key][0].setStyleSheet("background:#DDE5D8; border-color:#82927B;" if self.caps_lock else "")
            return
        aliases = {"BACKSPACE": "backspace", "DELETE": "delete", "ENTER": "enter", "SPACE": "space", "ESC": "esc", "PRTSC": "printscreen"}
        try:
            if key in aliases:
                self._send_input("press", aliases[key])
            elif key in ("LEFT", "RIGHT", "UP", "DOWN"):
                self._send_input("press", key.lower())
            elif self.modifiers:
                self._send_input("hotkey", *sorted(self.modifiers), key.lower())
                self.modifiers.clear()
                self._refresh_modifiers()
            elif len(key) == 1 and key.isalpha():
                self._send_input("write", key.upper() if self.caps_lock ^ ("shift" in self.modifiers) else key.lower())
                self.modifiers.discard("shift")
                self._refresh_modifiers()
            else:
                self._send_input("write", key.lower())
        except Exception as exc:
            self.on_status("Key action failed: {}".format(exc))

    def _send_input(self, method, *args, **kwargs):
        if self.input_adapter is None:
            return getattr(pyautogui, method)(*args, **kwargs)
        result = self.input_adapter(method, *args, **kwargs)
        if result is False:
            raise RuntimeError("The target application rejected keyboard input.")
        return result

    def _refresh_modifiers(self):
        labels = {"SHIFT": "shift", "CTRL": "ctrl", "ALT": "alt", "WIN": "win"}
        for name, modifier in labels.items():
            for button in self.buttons.get(name, []):
                button.setStyleSheet("background:#DDE5D8; border-color:#82927B;" if modifier in self.modifiers else "")

    def type_text(self, text):
        self._send_input("write", text, interval=0.02)
        self.on_status("Typed {} characters".format(len(text)))
        return True


if __name__ == "__main__":
    app = QApplication(sys.argv)
    keyboard = VirtualKeyboard()
    keyboard.show()
    sys.exit(app.exec_())
