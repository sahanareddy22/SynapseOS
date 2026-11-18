from PyQt5.QtWidgets import QWidget, QPushButton, QGridLayout
from PyQt5.QtCore import Qt
import pyautogui

class EyeKeyboard(QWidget):

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Eye Keyboard")
        self.setGeometry(300, 400, 600, 200)
        self.setWindowFlags(Qt.WindowStaysOnTopHint)

        layout = QGridLayout()
        self.setLayout(layout)

        keys = "QWERTYUIOPASDFGHJKLZXCVBNM"
        self.buttons = []

        row = 0
        col = 0

        for key in keys:
            btn = QPushButton(key)
            btn.setFixedSize(50, 50)
            layout.addWidget(btn, row, col)
            self.buttons.append(btn)

            col += 1
            if col > 9:
                col = 0
                row += 1

    def type_letter(self, letter):
        pyautogui.write(letter.lower())
