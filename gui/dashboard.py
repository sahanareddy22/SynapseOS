from PyQt5.QtWidgets import QWidget, QLabel, QVBoxLayout
from PyQt5.QtCore import Qt

class Dashboard(QWidget):

    def __init__(self):
        super().__init__()

        self.setWindowTitle("CHITTI")
        self.setGeometry(600, 250, 400, 200)
        self.setStyleSheet("background-color: black;")

        self.label = QLabel("🔴 Sleeping")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("color: white; font-size: 22px;")

        layout = QVBoxLayout()
        layout.addWidget(self.label)
        self.setLayout(layout)
