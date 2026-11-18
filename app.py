import sys

import cv2
import pyautogui
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (QApplication, QGridLayout, QHBoxLayout, QLabel,
                             QMainWindow, QPushButton, QVBoxLayout, QWidget)

from vision.camera_pipeline import CameraPipeline
from vision.eye_control import EyeController
from voice.assistant import VoiceAssistant
from vkeyboard import VirtualKeyboard


class UiBridge(QObject):
    frame = pyqtSignal(object)
    status = pyqtSignal(str)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SynapseOS | Hands-free computing")
        self.resize(1100, 720)
        self.setStyleSheet("QMainWindow,QWidget{background:#10151f;color:#e6edf7;font-family:'Segoe UI';} QPushButton{background:#202b3b;border:1px solid #35445a;border-radius:9px;padding:11px;color:#e6edf7;} QPushButton:hover{background:#2b3c54;} QLabel#title{font-size:26px;font-weight:600;} QLabel#card{background:#182231;border:1px solid #28384d;border-radius:12px;padding:14px;} QGridLayout{spacing:8px;}")
        self.bridge = UiBridge()
        self.bridge.frame.connect(self.show_frame)
        self.bridge.status.connect(self.show_status)
        self.camera = None
        self.keyboard = None
        self.emergency = False
        self.voice = VoiceAssistant(on_status=lambda text: self.bridge.status.emit(text), on_command=self.execute_voice)
        root = QWidget()
        layout = QVBoxLayout(root)
        header = QHBoxLayout()
        title = QLabel("SynapseOS")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch()
        self.status = QLabel("Ready · camera idle · microphone idle")
        header.addWidget(self.status)
        layout.addLayout(header)
        content = QHBoxLayout()
        self.preview = QLabel("Camera preview\n\nStart eye tracking to enable camera")
        self.preview.setObjectName("card")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(640, 420)
        content.addWidget(self.preview, 3)
        panel = QVBoxLayout()
        panel.addWidget(self.card("Live controls"))
        for text, callback in (("Start eye tracking", self.start_eye), ("Calibrate gaze", self.calibrate), ("Start voice control", self.start_voice), ("Stop voice control", self.stop_voice), ("Open virtual keyboard", self.open_keyboard), ("Settings · blink click enabled", self.show_settings)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            panel.addWidget(button)
        panel.addStretch()
        content.addLayout(panel, 1)
        layout.addLayout(content)
        footer = QHBoxLayout()
        footer.addWidget(QLabel("Commands: open notepad · click · scroll up · type hello · press enter"))
        footer.addStretch()
        stop = QPushButton("EMERGENCY STOP")
        stop.setStyleSheet("background:#8d2936;font-weight:600;")
        stop.clicked.connect(self.emergency_stop)
        footer.addWidget(stop)
        layout.addLayout(footer)
        self.setCentralWidget(root)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(1000)

    @staticmethod
    def card(text):
        label = QLabel(text)
        label.setObjectName("card")
        return label

    def show_status(self, text):
        self.status.setText(text)

    def show_frame(self, frame):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width, channels = rgb.shape
        image = QImage(rgb.data, width, height, channels * width, QImage.Format_RGB888).copy()
        self.preview.setPixmap(QPixmap.fromImage(image).scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def start_eye(self):
        if self.emergency:
            self.show_status("Reset the app to clear emergency stop")
            return
        if self.camera and self.camera.running:
            return
        controller = EyeController(on_status=lambda text: self.bridge.status.emit(text))
        self.camera = CameraPipeline(controller, self.bridge.frame.emit, self.bridge.status.emit)
        self.camera.start()

    def calibrate(self):
        if self.camera:
            self.camera.calibrate()
        else:
            self.show_status("Start eye tracking before calibration")

    def start_voice(self):
        if not self.emergency:
            self.voice.start()

    def stop_voice(self):
        self.voice.stop()

    def open_keyboard(self):
        if self.keyboard is None:
            self.keyboard = VirtualKeyboard(on_status=lambda text: self.bridge.status.emit(text))
        self.keyboard.show()
        self.keyboard.raise_()

    def execute_voice(self, text):
        if self.emergency:
            return False
        try:
            command = text.lower().strip()
            if command.startswith("type "):
                return self.keyboard_type(text[5:].strip())
            if command in ("minimize window", "minimise window", "minimize this window", "minimise this window"):
                self.showMinimized()
                return True
            if command in ("maximize window", "maximise window", "maximize this window", "maximise this window"):
                self.showMaximized()
                return True
            if command in ("restore window", "restore this window"):
                self.showNormal()
                return True
            if command in ("close window", "close this window"):
                self.close()
                return True
            if command == "open notepad":
                import subprocess
                subprocess.Popen(["notepad.exe"])
                return True
            if command == "open calculator":
                import subprocess
                subprocess.Popen(["calc.exe"])
                return True
            if command == "open file explorer":
                import subprocess
                subprocess.Popen(["explorer.exe"])
                return True
            if command == "open chrome":
                import webbrowser
                webbrowser.open("https://www.google.com")
                return True
            actions = {
                "minimize window": self.showMinimized,
                "minimise window": self.showMinimized,
                "minimize this window": self.showMinimized,
                "minimise this window": self.showMinimized,
                "maximize window": self.showMaximized,
                "maximise window": self.showMaximized,
                "restore window": self.showNormal,
                "close window": self.close,
                "show desktop": lambda: pyautogui.hotkey("win", "d"),
                "switch window": lambda: pyautogui.hotkey("alt", "tab"),
                "copy": lambda: pyautogui.hotkey("ctrl", "c"),
                "paste": lambda: pyautogui.hotkey("ctrl", "v"),
                "cut": lambda: pyautogui.hotkey("ctrl", "x"),
                "undo": lambda: pyautogui.hotkey("ctrl", "z"),
                "select all": lambda: pyautogui.hotkey("ctrl", "a"),
                "save": lambda: pyautogui.hotkey("ctrl", "s"),
                "press tab": lambda: pyautogui.press("tab"),
                "press backspace": lambda: pyautogui.press("backspace"),
                "press delete": lambda: pyautogui.press("delete"),
                "press space": lambda: pyautogui.press("space"),
                "press escape": lambda: pyautogui.press("esc"),
                "left click": lambda: pyautogui.click(),
                "right click": lambda: pyautogui.click(button="right"),
                "double click": lambda: pyautogui.doubleClick(),
                "open keyboard": self.open_keyboard,
                "start eye tracking": self.start_eye,
                "calibrate gaze": self.calibrate,
                "stop voice control": self.stop_voice,
                "emergency stop": self.emergency_stop,
            }
            action = actions.get(command)
            if action:
                action()
                return True
            return False
        except Exception as exc:
            self.bridge.status.emit("Voice action failed: {}".format(exc))
            return False

    def keyboard_type(self, text):
        if self.keyboard is None:
            self.open_keyboard()
        return self.keyboard.type_text(text)

    def show_settings(self):
        self.show_status("Privacy: camera and microphone run only while started; emergency stop releases both")

    def refresh_status(self):
        if self.camera and not self.camera.running and self.camera.thread and not self.camera.thread.is_alive():
            self.camera = None

    def emergency_stop(self):
        self.emergency = True
        self.voice.stop()
        if self.camera:
            self.camera.stop()
            self.camera = None
        if self.keyboard:
            self.keyboard.close()
            self.keyboard = None
        self.preview.setPixmap(QPixmap())
        self.preview.setText("Emergency stop active")
        self.show_status("All controls stopped")

    def closeEvent(self, event):
        self.voice.stop()
        if self.camera:
            self.camera.stop()
        if self.keyboard:
            self.keyboard.close()
        event.accept()


if __name__ == "__main__":
    pyautogui.FAILSAFE = True
    application = QApplication(sys.argv)
    window = Window()
    window.show()
    sys.exit(application.exec_())
