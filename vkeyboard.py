import sys

import pyautogui
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QGridLayout, QPushButton, QWidget


class VirtualKeyboard(QWidget):
    def __init__(self, on_status=None, on_select=None):
        super().__init__()
        self.setWindowTitle("SynapseOS Virtual Keyboard")
        self.setWindowFlags(Qt.WindowStaysOnTopHint)
        self.on_status = on_status or (lambda _message: None)
        self.on_select = on_select or (lambda _key: None)
        self.modifiers = set()
        self.buttons = {}
        self.setStyleSheet("QWidget{background:#131b27;color:#edf3fc;} QPushButton{background:#263449;border:1px solid #40516a;border-radius:6px;font-size:15px;font-weight:600;} QPushButton:pressed{background:#3879bf;}")
        grid = QGridLayout(self)
        grid.setSpacing(5)
        rows = [
            ["ESC", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "PRTSC", "DELETE"],
            ["`", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "=", "BACKSPACE"],
            ["TAB", "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P", "[", "]", "\\"],
            ["CAPSLOCK", "A", "S", "D", "F", "G", "H", "J", "K", "L", ";", "'", "ENTER"],
            ["SHIFT", "Z", "X", "C", "V", "B", "N", "M", ",", ".", "/", "SHIFT"],
            ["CTRL", "WIN", "ALT", "SPACE", "ALT", "CTRL", "LEFT", "UP", "DOWN", "RIGHT"],
        ]
        self.caps_lock = False
        for row_index, row in enumerate(rows):
            for col_index, key in enumerate(row):
                button = QPushButton(key)
                button.setFocusPolicy(Qt.NoFocus)
                button.setMinimumSize(43, 48)
                span = 4 if key == "SPACE" else 2 if key in ("BACKSPACE", "CAPSLOCK", "ENTER", "SHIFT", "CTRL", "TAB") else 1
                button.clicked.connect(lambda _checked=False, selected=key: self.key_pressed(selected))
                grid.addWidget(button, row_index, col_index, 1, span)
                self.buttons.setdefault(key, []).append(button)
        self.resize(980, 360)

    def key_pressed(self, key):
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
            self.buttons[key][0].setStyleSheet("background:#3267a8" if self.caps_lock else "")
            return
        aliases = {"BACKSPACE": "backspace", "DELETE": "delete", "ENTER": "enter", "SPACE": "space", "ESC": "esc", "PRTSC": "printscreen"}
        try:
            if key in aliases:
                pyautogui.press(aliases[key])
            elif key in ("LEFT", "RIGHT", "UP", "DOWN"):
                pyautogui.press(key.lower())
            elif self.modifiers:
                pyautogui.hotkey(*sorted(self.modifiers), key.lower())
                self.modifiers.clear()
                self._refresh_modifiers()
            elif len(key) == 1 and key.isalpha():
                pyautogui.write(key.upper() if self.caps_lock ^ ("shift" in self.modifiers) else key.lower())
                self.modifiers.discard("shift")
                self._refresh_modifiers()
            else:
                pyautogui.write(key.lower())
        except Exception as exc:
            self.on_status("Key action failed: {}".format(exc))

    def _refresh_modifiers(self):
        labels = {"SHIFT": "shift", "CTRL": "ctrl", "ALT": "alt", "WIN": "win"}
        for name, modifier in labels.items():
            for button in self.buttons.get(name, []):
                button.setStyleSheet("background:#3267a8" if modifier in self.modifiers else "")

    def type_text(self, text):
        pyautogui.write(text, interval=0.02)
        self.on_status("Typed {} characters".format(len(text)))
        return True


if __name__ == "__main__":
    app = QApplication(sys.argv)
    keyboard = VirtualKeyboard()
    keyboard.show()
    sys.exit(app.exec_())
