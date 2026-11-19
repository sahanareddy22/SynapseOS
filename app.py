import sys
import logging
import threading
import time
import webbrowser

import numpy as np
import pyautogui
# Initialize MediaPipe's native bindings before Qt loads its bundled DLLs.
# On some Windows installations the reverse order makes _framework_bindings
# fail during DLL initialization.
from vision.eye_control import EyeController

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread, pyqtSlot
from PyQt5.QtGui import QImage, QPixmap, QColor
from PyQt5.QtWidgets import (QApplication, QGridLayout, QHBoxLayout, QLabel,
                             QMainWindow, QPushButton, QVBoxLayout, QWidget,
                             QStackedWidget, QListWidget, QListWidgetItem)

from desktop_input import DEFAULT_INPUT_COORDINATOR
from vision.camera_pipeline import CameraPipeline
from vision.frame_utils import validate_canonical_bgr_frame
from voice.assistant import VoiceAssistant
from voice.catalog import command_catalog
from voice.gui_request import VoiceActionRequest
from voice.windows_adapter import WindowsControlAdapter
from vkeyboard import VirtualKeyboard

logger = logging.getLogger("voice.app")


class UiBridge(QObject):
    frame = pyqtSignal(object)
    status = pyqtSignal(str)
    voice_action = pyqtSignal(object)
    clipboard_request = pyqtSignal(object)
    voice_event = pyqtSignal(str)


class _ClipboardRequest:
    def __init__(self, operation, value=None):
        self.operation = operation
        self.value = value
        self.result = None
        self.error = None
        self.done = threading.Event()


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SynapseOS | Hands-free computing")
        self.resize(1240, 820)
        self.setMinimumSize(980, 680)
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #F7F6F2; color: #353630; font-family: 'Segoe UI'; font-size: 10pt; }
            QWidget#sidebar { background: #EEECE5; border-right: 1px solid #E2E0D8; }
            QLabel#brand { font-size: 19pt; font-weight: 650; color: #353630; padding: 10px 4px; }
            QLabel#pageTitle { font-size: 19pt; font-weight: 600; color: #353630; }
            QLabel#statusBar, QLabel#surface { background: #FFFFFF; border: 1px solid #E2E0D8; border-radius: 7px; padding: 10px; }
            QLabel#cameraStatus, QLabel#voiceStatus { background: #FFFFFF; border: 1px solid #E2E0D8; border-radius: 14px; padding: 6px 12px; }
            QLabel#card { background: #FFFFFF; border: 1px solid #E2E0D8; border-radius: 8px; padding: 14px; }
            QListWidget { background: #FFFFFF; alternate-background-color: #F2F1EC; color: #353630;
                          border: 1px solid #E2E0D8; border-radius: 7px; padding: 8px; }
            QListWidget::item { padding: 9px 8px; border-bottom: 1px solid #EEECE5; }
            QListWidget::item:selected { background: #E3E9DE; color: #3F503A; }
            QPushButton { background: #FFFFFF; border: 1px solid #D8D6CE; border-radius: 6px; padding: 10px 12px; color: #353630; text-align: left; }
            QPushButton:hover { background: #F0EFE9; border-color: #A9B2A2; }
            QPushButton:pressed { background: #E5E9E1; }
            QPushButton:disabled { color: #A5A49C; background: #F1F0EB; }
            QPushButton#navButton { border: none; background: transparent; padding: 11px 12px; }
            QPushButton#navButton:hover, QPushButton#navButton[active="true"] { background: #E0E5DC; color: #465642; }
            QPushButton#emergencyButton { background: #A64B45; color: #FFFFFF; border: 1px solid #93413C; font-weight: 600; }
            QPushButton#emergencyButton:hover { background: #93413C; }
            QLabel#secondary { color: #77776F; }
            QStackedWidget { background: #F7F6F2; }
            QGridLayout { spacing: 8px; }
        """)
        self.bridge = UiBridge()
        self.bridge.frame.connect(self.show_frame)
        self.bridge.status.connect(self.show_status)
        self.bridge.voice_action.connect(self._run_queued_voice_action)
        self.bridge.clipboard_request.connect(self._run_clipboard_request)
        self.bridge.voice_event.connect(self._on_voice_event)
        self.camera = None
        self._camera_connected = False
        self._camera_calibrating = False
        self._camera_stop_requested = False
        self._camera_terminal_status = None
        self._shutdown_pending = False
        self._last_preview_emit = 0.0
        self._preview_emit_lock = threading.Lock()
        self.keyboard = None
        self._keyboard_target = None
        self.emergency = False
        self.input_coordinator = DEFAULT_INPUT_COORDINATOR
        self._pending_voice_eye_start = False
        self._pending_voice_eye_stop = False
        self._pending_voice_calibration = False
        self._voice_action_lock = threading.Lock()
        self._voice_action_requests = set()
        self.action_adapter = WindowsControlAdapter(browser_open=webbrowser.open,
                                                    clipboard=QApplication.clipboard(),
                                                    input_coordinator=self.input_coordinator)
        # Capture an external foreground HWND before the dashboard is shown.
        # Later, dashboard activation must not replace that retained target.
        self._capture_keyboard_target()
        application = getattr(QApplication, "instance", lambda: None)()
        state_signal = getattr(application, "applicationStateChanged", None)
        if state_signal is not None and callable(getattr(state_signal, "connect", None)):
            state_signal.connect(self._on_application_state_changed)
        self.voice = VoiceAssistant(on_status=lambda text: self.bridge.voice_event.emit(text),
                                    action_executor=self.request_voice_action,
                                    cancel_executor=self.cancel_voice_actions)
        self._camera_state = "Stopped"
        self._voice_state = "Stopped"
        shell = QWidget()
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(218)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(18, 20, 18, 18)
        side_layout.setSpacing(8)
        brand = QLabel("SynapseOS")
        brand.setObjectName("brand")
        side_layout.addWidget(brand)
        side_layout.addSpacing(12)
        self.page_stack = QStackedWidget()
        self.pages = {}
        self.navigation_buttons = {}
        self.page_title = QLabel("Dashboard")
        self.page_title.setObjectName("pageTitle")
        for page_id, label in (("dashboard", "Dashboard"), ("eye", "Eye Tracking"),
                               ("voice", "Voice Control"), ("activity", "Activity"),
                               ("settings", "Settings")):
            button = QPushButton(label)
            button.setObjectName("navButton")
            button.setProperty("active", page_id == "dashboard")
            button.clicked.connect(lambda _checked=False, selected=page_id: self.navigate_to(selected))
            side_layout.addWidget(button)
            self.navigation_buttons[page_id] = button
        side_layout.addStretch()
        emergency_button = QPushButton("Emergency Stop")
        emergency_button.setObjectName("emergencyButton")
        emergency_button.clicked.connect(self.emergency_stop)
        side_layout.addWidget(emergency_button)
        self.emergency_button = emergency_button
        shell_layout.addWidget(sidebar)

        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(28, 22, 28, 24)
        workspace_layout.setSpacing(18)
        header = QHBoxLayout()
        header.addWidget(self.page_title)
        header.addStretch()
        self.camera_status = QLabel("Camera · Stopped")
        self.camera_status.setObjectName("cameraStatus")
        header.addWidget(self.camera_status)
        self.voice_status = QLabel("Voice · Stopped")
        self.voice_status.setObjectName("voiceStatus")
        header.addWidget(self.voice_status)
        workspace_layout.addLayout(header)
        self.status = QLabel("Ready")
        self.status.setObjectName("statusBar")
        workspace_layout.addWidget(self.status)

        dashboard_page = QWidget()
        layout = QVBoxLayout(dashboard_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        content = QHBoxLayout()
        self.preview = QLabel("Camera preview\n\nStart eye tracking to enable camera")
        self.preview.setObjectName("card")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(560, 390)
        content.addWidget(self.preview, 3)
        panel = QVBoxLayout()
        panel.addWidget(self.card("Live controls"))
        self.start_eye_button = QPushButton("Start eye tracking")
        self.start_eye_button.clicked.connect(self.start_eye)
        panel.addWidget(self.start_eye_button)
        self.stop_eye_button = QPushButton("Stop eye tracking")
        self.stop_eye_button.clicked.connect(self.stop_eye)
        panel.addWidget(self.stop_eye_button)
        self.calibrate_button = QPushButton("Calibrate gaze")
        self.calibrate_button.clicked.connect(self.calibrate)
        panel.addWidget(self.calibrate_button)
        for text, callback in (("Start voice control", self.start_voice), ("Stop voice control", self.stop_voice), ("Open virtual keyboard", self.open_keyboard), ("Settings · blink click enabled", self.show_settings)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            panel.addWidget(button)
        panel.addStretch()
        content.addLayout(panel, 1)
        layout.addLayout(content)
        footer = QLabel("Voice commands and eye actions are coordinated through SynapseOS.")
        footer.setObjectName("secondary")
        layout.addWidget(footer)
        self.pages["dashboard"] = dashboard_page
        self.page_stack.addWidget(dashboard_page)
        for page_id, title_text, description in (
                ("eye", "Eye Tracking", "Camera preview and tracking controls are available on Dashboard."),
                ("activity", "Activity", "Service status and recent activity will appear here."),
                ("settings", "Settings", "Current settings and privacy information are available from Dashboard.")):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            page_layout.addWidget(self.card(description))
            page_layout.addStretch()
            self.pages[page_id] = page
            self.page_stack.addWidget(page)
        self.pages["voice"] = self._build_voice_page()
        self.page_stack.addWidget(self.pages["voice"])
        workspace_layout.addWidget(self.page_stack, 1)
        shell_layout.addWidget(workspace, 1)
        self.setCentralWidget(shell)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(1000)
        self._update_eye_buttons()
        self._refresh_service_indicators()

    def _build_voice_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        sections = QHBoxLayout()
        sections.setSpacing(16)

        console = QWidget()
        console_layout = QVBoxLayout(console)
        console_layout.setContentsMargins(0, 0, 0, 0)
        console_layout.addWidget(self.card("Live Voice Console"))
        self.voice_empty_state = QLabel("No voice events yet. Start Voice Control to begin.")
        self.voice_empty_state.setObjectName("secondary")
        self.voice_empty_state.setAlignment(Qt.AlignCenter)
        console_layout.addWidget(self.voice_empty_state)
        self.voice_events = QListWidget()
        self.voice_events.setObjectName("voiceEvents")
        self.voice_events.setAlternatingRowColors(True)
        self.voice_events.setWordWrap(True)
        self.voice_events.setUniformItemSizes(False)
        console_layout.addWidget(self.voice_events, 1)
        sections.addWidget(console, 7)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(self.card("Voice Controls"))
        self.voice_page_status = QLabel("Stopped")
        self.voice_page_status.setObjectName("surface")
        controls_layout.addWidget(self.voice_page_status)
        self.voice_start_button = QPushButton("Start Voice Control")
        self.voice_start_button.clicked.connect(self.start_voice)
        controls_layout.addWidget(self.voice_start_button)
        self.voice_stop_button = QPushButton("Stop Voice Control")
        self.voice_stop_button.clicked.connect(self.stop_voice)
        controls_layout.addWidget(self.voice_stop_button)
        try:
            wake_word = self.voice.wake_word
        except AttributeError:
            wake_word = "Chitti"
        self.wake_word_status = QLabel("Wake word · {}".format(str(wake_word).title()))
        self.wake_word_status.setObjectName("secondary")
        controls_layout.addWidget(self.wake_word_status)
        controls_layout.addWidget(self.card("Supported command examples"))
        self.voice_command_examples = QListWidget()
        self.voice_command_examples.setObjectName("voiceCommandExamples")
        for intent_name, phrases in sorted(command_catalog().items()):
            if intent_name.startswith("app.target.") or not phrases:
                continue
            self.voice_command_examples.addItem("{}  ·  {}".format(intent_name, phrases[0]))
        controls_layout.addWidget(self.voice_command_examples, 1)
        sections.addWidget(controls, 3)
        outer.addLayout(sections, 1)
        return page

    def _on_voice_event(self, message):
        """Render only events emitted by VoiceAssistant's existing status callback."""
        message = str(message)
        self.show_status(message)
        if message.startswith("Heard: "):
            role = "YOU · RECOGNIZED"
            body = message[len("Heard: "):]
            self._voice_state = "Processing"
            self.voice_page_status.setText("Processing recognized command")
        else:
            role = "ASSISTANT · STATUS / RESPONSE"
            body = message
            normalized = message.casefold()
            if "microphone is unavailable" in normalized or "could not start microphone" in normalized:
                self._voice_state = "Error"
                self.voice_page_status.setText("Microphone unavailable")
            elif "stopped" in normalized:
                self._voice_state = "Stopped"
                self.voice_page_status.setText("Stopped")
            elif "paused" in normalized:
                self._voice_state = "Paused"
                self.voice_page_status.setText("Paused")
            elif "starting" in normalized:
                self._voice_state = "Starting"
                self.voice_page_status.setText("Starting")
            else:
                self._voice_state = "Listening"
                self.voice_page_status.setText("Listening · response received")
        from datetime import datetime
        event_item = QListWidgetItem("{}  {}\n{}".format(datetime.now().strftime("%H:%M:%S"), role, body))
        event_item.setBackground(QColor("#E9EEE5" if role.startswith("YOU") else "#FFFFFF"))
        event_item.setForeground(QColor("#353630"))
        self.voice_events.addItem(event_item)
        self.voice_empty_state.hide()
        self.voice_events.scrollToBottom()
        self._refresh_service_indicators()

    def navigate_to(self, page_id):
        """Show a persistent page; navigation never starts or stops services."""
        if page_id not in self.pages:
            return False
        self.page_stack.setCurrentWidget(self.pages[page_id])
        titles = {"dashboard": "Dashboard", "eye": "Eye Tracking", "voice": "Voice Control",
                  "activity": "Activity", "settings": "Settings"}
        self.page_title.setText(titles[page_id])
        for key, button in self.navigation_buttons.items():
            button.setProperty("active", key == page_id)
            button.style().unpolish(button)
            button.style().polish(button)
        return True

    def _refresh_service_indicators(self):
        if self._camera_stop_requested and self.camera is not None:
            thread = self.camera.thread
            self._camera_state = "Stopping" if thread is not None and thread.is_alive() else "Stopped"
        elif self._camera_terminal_status:
            self._camera_state = self._camera_terminal_status.replace("Camera error · ", "Error · ")
        elif self._camera_calibrating:
            self._camera_state = "Calibrating"
        elif self._camera_connected:
            self._camera_state = "Connected"
        elif self.camera is not None and self.camera.running:
            self._camera_state = "Starting"
        else:
            self._camera_state = "Stopped"
        self.camera_status.setText("Camera · {}".format(self._camera_state))
        self.voice_status.setText("Voice · {}".format(self._voice_state))
        positive = "background:#E7EEE4;color:#4D6247;border:1px solid #CFD9C9;border-radius:14px;padding:6px 12px;"
        pending = "background:#F4EEDF;color:#77653D;border:1px solid #E6DCC1;border-radius:14px;padding:6px 12px;"
        negative = "background:#F4E7E4;color:#8D4942;border:1px solid #E7D0CC;border-radius:14px;padding:6px 12px;"
        neutral = "background:#FFFFFF;color:#77776F;border:1px solid #E2E0D8;border-radius:14px;padding:6px 12px;"
        self.camera_status.setStyleSheet(positive if self._camera_state == "Connected" else
                                         pending if self._camera_state in {"Starting", "Reconnecting", "Calibrating", "Stopping"} else
                                         negative if self._camera_state in {"Unavailable", "Error"} else neutral)
        self.voice_status.setStyleSheet(positive if self._voice_state == "Listening" else
                                        pending if self._voice_state in {"Starting", "Processing"} else
                                        negative if self._voice_state in {"Error", "Emergency stopped"} else neutral)

    @staticmethod
    def card(text):
        label = QLabel(text)
        label.setObjectName("card")
        return label

    def show_status(self, text):
        message = str(text)
        lower = message.lower()
        if (lower.startswith("camera") or lower.startswith("calibration")
                or "camera worker" in lower or "gaze calibration" in lower
                or "five-point" in lower):
            if "still stopping" in lower:
                self._camera_state = "Stopping"
            elif "unavailable" in lower or "camera error" in lower or "cleanup failed" in lower:
                self._camera_state = "Unavailable" if "unavailable" in lower else "Error"
            elif "connected" in lower or "reconnected" in lower:
                self._camera_state = "Connected"
            elif "calibrat" in lower or "look at the screen" in lower:
                self._camera_state = "Calibrating"
            elif "stopped" in lower:
                self._camera_state = "Stopped"
            elif "starting" in lower or "startup" in lower or "reconnect" in lower:
                self._camera_state = "Starting" if "reconnect" not in lower else "Reconnecting"
        if any(token in lower for token in ("voice listener", "voice listening", "listening paused", "speech recognition", "microphone", "say chitti")):
            if "still stopping" in lower:
                self._voice_state = "Stopping"
            elif "stopped" in lower:
                self._voice_state = "Stopped"
            elif "paused" in lower:
                self._voice_state = "Paused"
            elif "starting" in lower or "resumed" in lower:
                self._voice_state = "Starting" if "starting" in lower else "Listening"
            elif "listening" in lower or "say chitti" in lower:
                self._voice_state = "Listening"
        if message == "Starting camera":
            self._camera_connected = False
            self._camera_calibrating = False
            self._camera_terminal_status = None
        elif message == "Camera connected" or message == "Camera reconnected":
            self._camera_connected = True
            self._camera_terminal_status = None
            if self._pending_voice_eye_start:
                self._pending_voice_eye_start = False
                self._voice_feedback("Eye tracking started; camera connected.")
        elif "reconnect" in lower or "disconnected" in lower or "frame unavailable" in lower:
            self._camera_connected = False
            if "camera" in lower:
                message = "Reconnecting to camera"
        elif "camera unavailable" in lower or "recovery failed" in lower or "recovery attempts exhausted" in lower:
            self._camera_connected = False
            message = "Camera unavailable"
            if "exhausted" in lower or "recovery failed" in lower:
                self._camera_terminal_status = "Camera unavailable"
                if self._pending_voice_eye_start:
                    self._pending_voice_eye_start = False
                    self._voice_feedback("Camera unavailable; eye tracking did not start.")
        elif lower.startswith("camera error"):
            self._camera_connected = False
            self._camera_terminal_status = "Camera error · " + message.partition(":")[2].strip()
            message = self._camera_terminal_status
        elif "cleanup failed" in lower:
            self._camera_connected = False
            self._camera_terminal_status = "Camera error · " + message
            message = self._camera_terminal_status
        elif message == "Camera stopped":
            self._camera_connected = False
            self._camera_calibrating = False
        elif "five-point" in lower and "complete" in lower:
            self._camera_calibrating = False
            message = "Calibration completed"
            if self._pending_voice_calibration:
                self._pending_voice_calibration = False
                self._voice_feedback("Gaze calibration completed.")
        elif any(marker in lower for marker in (
                "captured.", "unstable", "needs a recent", "calibration failed", "calibration skipped",
                "gaze control remains disabled", "calibration points were inconsistent")):
            self._camera_calibrating = False
            if self._pending_voice_calibration and any(marker in lower for marker in (
                    "unstable", "needs a recent", "calibration failed", "calibration skipped",
                    "gaze control remains disabled", "calibration points were inconsistent")):
                self._pending_voice_calibration = False
                self._voice_feedback("Gaze calibration was not accepted. Please try again when your eyes are visible and steady.")
        elif "calibration request queued" in lower:
            self._camera_calibrating = True
            message = "Calibrating gaze"
        elif ("calibration" in lower or "look at the screen" in lower or "keep looking" in lower) and (
                "sample" in lower or "queued" in lower or "keep looking" in lower):
            self._camera_calibrating = True
            message = "Calibration progress · " + message
        if message == "Camera stopped" and self.camera and self.camera.thread and self.camera.thread.is_alive():
            self._camera_stop_requested = True
            message = "Camera worker is still stopping"
        elif message == "Camera stopped" and self.camera is None:
            message = "Stopped"
        self.status.setText(message)
        self._update_eye_buttons()
        self._refresh_service_indicators()

    def _voice_feedback(self, message):
        try:
            self.voice._feedback(message)
        except Exception:
            logger.exception("Dashboard voice feedback failed")

    def show_frame(self, frame):
        try:
            bgr = validate_canonical_bgr_frame(frame)
            rgb = np.array(bgr[:, :, ::-1], dtype=np.uint8, copy=True, order="C")
            height, width, _channels = rgb.shape
            image = QImage(rgb.data, width, height, rgb.strides[0], QImage.Format_RGB888).copy()
            pixmap = QPixmap.fromImage(image).scaled(
                self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        except Exception as exc:
            logger.exception("Camera preview conversion failed at stage=qt_preview")
            self.show_status("Camera preview error: {}".format(type(exc).__name__))
            return
        self.preview.setPixmap(pixmap)

    def _emit_camera_preview(self, frame):
        now = time.monotonic()
        with self._preview_emit_lock:
            if now - self._last_preview_emit < 1.0 / 15.0:
                return
            self._last_preview_emit = now
        self.bridge.frame.emit(frame)

    def start_eye(self, voice_requested=False):
        if self.emergency:
            message = "Emergency stop is active; restart the app to enable eye tracking."
            self.show_status(message)
            return {"status": "failed", "message": message} if voice_requested else False
        if self.camera is not None:
            worker = self.camera.thread
            if worker is not None and worker.is_alive():
                if not self.camera.running:
                    message = "Camera worker is still stopping"
                    self.show_status(message)
                    return {"status": "action_requested", "message": message} if voice_requested else False
                if self._camera_connected:
                    message = "Eye tracking is already active."
                    self.show_status(message)
                    return {"status": "executed", "message": message} if voice_requested else True
                message = "Camera startup is already in progress."
                self.show_status(message)
                return {"status": "action_requested", "message": message} if voice_requested else True
            self.camera = None
        self._camera_connected = False
        self._camera_calibrating = False
        self._camera_stop_requested = False
        self._camera_terminal_status = None
        self._last_preview_emit = 0.0
        self._pending_voice_eye_start = bool(voice_requested)
        self.input_coordinator.resume_eye()
        self.show_status("Starting camera")
        try:
            controller_factory = lambda: EyeController(
                on_status=lambda text: self.bridge.status.emit(text),
                input_coordinator=self.input_coordinator,
            )
            self.camera = CameraPipeline(
                on_frame=self._emit_camera_preview,
                on_status=self.bridge.status.emit,
                controller_factory=controller_factory,
            )
            if not self.camera.start():
                self._camera_terminal_status = "Camera error · could not start camera worker"
                self._pending_voice_eye_start = False
                self.show_status(self._camera_terminal_status)
                return {"status": "failed", "message": self._camera_terminal_status} if voice_requested else False
        except Exception as exc:
            logger.exception("Eye tracking startup failed")
            self.camera = None
            self._camera_terminal_status = "Camera error · {}".format(type(exc).__name__)
            self._pending_voice_eye_start = False
            self.show_status(self._camera_terminal_status)
            return {"status": "failed", "message": self._camera_terminal_status} if voice_requested else False
        self._update_eye_buttons()
        message = "Eye-tracking startup requested; waiting for a valid camera frame."
        return {"status": "action_requested", "message": message} if voice_requested else True

    def stop_eye(self, voice_requested=False):
        if self.camera is None:
            self._camera_connected = False
            self._camera_terminal_status = "Stopped"
            self.show_status("Stopped")
            message = "Eye tracking is already stopped."
            return {"status": "executed", "message": message} if voice_requested else True
        self.input_coordinator.pause_eye()
        controller = self.camera.eye_controller
        if controller is not None:
            controller.pause_tracking()
        self._camera_stop_requested = True
        self._camera_calibrating = False
        self._pending_voice_eye_stop = bool(voice_requested)
        self._update_eye_buttons()
        try:
            stopped = self.camera.stop(timeout=0)
        except Exception:
            logger.exception("Eye tracking stop request failed")
            stopped = False
        if stopped:
            self.camera = None
            self._camera_connected = False
            self._camera_terminal_status = "Stopped"
            self._pending_voice_eye_stop = False
            self.show_status("Stopped")
            message = "Eye tracking stopped."
            return {"status": "executed", "message": message} if voice_requested else True
        else:
            self.show_status("Camera worker is still stopping")
            message = "Eye tracking stop requested; camera worker is still stopping."
            return {"status": "action_requested", "message": message} if voice_requested else False

    def pause_eye(self, voice_requested=False):
        if self.camera is None or not self.camera.running:
            message = "Eye tracking is not running."
            self.show_status(message)
            return {"status": "failed", "message": message} if voice_requested else False
        controller = self.camera.eye_controller
        if controller is not None and not controller.pause_tracking():
            message = "Eye tracking cannot be paused while emergency stop is active."
            self.show_status(message)
            return {"status": "failed", "message": message} if voice_requested else False
        if controller is None and not self.input_coordinator.pause_eye():
            message = "Eye tracking cannot be paused while emergency stop is active."
            self.show_status(message)
            return {"status": "failed", "message": message} if voice_requested else False
        message = "Eye control paused; camera capture and preview remain active."
        self.show_status(message)
        return {"status": "executed", "message": message} if voice_requested else True

    def resume_eye(self, voice_requested=False):
        if (self.camera is None or not self.camera.running or not self._camera_connected
                or self.camera.eye_controller is None):
            message = "Eye tracking is not connected; start it before resuming eye control."
            self.show_status(message)
            return {"status": "failed", "message": message} if voice_requested else False
        if not self.camera.eye_controller.resume_tracking():
            message = "Eye control cannot resume while emergency stop is active."
            self.show_status(message)
            return {"status": "failed", "message": message} if voice_requested else False
        message = "Eye control resumed; calibration was preserved."
        self.show_status(message)
        return {"status": "executed", "message": message} if voice_requested else True

    def calibrate(self, voice_requested=False):
        worker = self.camera.thread if self.camera is not None else None
        if (self.camera is None or not self._camera_connected or not self.camera.running
                or worker is None or not worker.is_alive()):
            message = "Calibration unavailable · camera is not connected"
            self.show_status(message)
            return {"status": "failed", "message": message} if voice_requested else False
        queued = self.camera.calibrate()
        if not queued:
            message = "Calibration request was rejected; wait for a recent valid eye frame."
            self.show_status(message)
            return {"status": "failed", "message": message} if voice_requested else False
        else:
            self._camera_calibrating = True
            self._pending_voice_calibration = bool(voice_requested)
        self._update_eye_buttons()
        message = "Gaze calibration request accepted; follow the on-screen target prompts."
        return {"status": "action_requested", "message": message} if voice_requested else True

    def _update_eye_buttons(self):
        if not hasattr(self, "start_eye_button"):
            return
        worker_alive = bool(self.camera is not None and self.camera.thread is not None
                            and self.camera.thread.is_alive())
        active = bool(worker_alive and self.camera.running)
        self.start_eye_button.setEnabled(not self.emergency and not worker_alive)
        self.stop_eye_button.setEnabled(active)
        self.calibrate_button.setEnabled(
            not self.emergency and active and self._camera_connected and not self._camera_calibrating
        )

    def start_voice(self):
        if not self.emergency:
            self.voice.start()

    def stop_voice(self):
        self.voice.stop()

    def cancel_voice_actions(self):
        if getattr(getattr(self, "voice", None), "dispatcher", None) is not None and self.voice.dispatcher.emergency_stopped:
            self.input_coordinator.activate_emergency()
        self.input_coordinator.cancel_voice_actions()
        with self._voice_action_lock:
            requests = tuple(self._voice_action_requests)
        for request in requests:
            request.cancel_if_pending()
        self.action_adapter.cancel_all()

    def pause_voice(self):
        self.voice.dispatcher.listening_enabled = False
        self.voice.dispatcher.cancel_all()
        message = "Listening paused; say Chitti, start listening, or press Start voice control."
        self.show_status(message)
        return {"status": "executed", "message": message}

    def open_keyboard(self):
        keyboard_visible = bool(self.keyboard and getattr(self.keyboard, "isVisible", lambda: False)())
        if not keyboard_visible:
            self._capture_keyboard_target()
        if self.keyboard is None:
            self.keyboard = VirtualKeyboard(
                on_status=lambda text: self.bridge.status.emit(text),
                input_adapter=self._dispatch_virtual_keyboard_input,
            )
        self.keyboard.show()
        self.keyboard.raise_()
        try:
            geometry = self.keyboard.frameGeometry()
            ratio = self.keyboard.devicePixelRatioF()
            enabled = sum(button.isEnabled()
                          for buttons in self.keyboard.buttons.values() for button in buttons)
            logger.info("VKEYBOARD shown geometry=%s device_pixel_ratio=%s enabled_buttons=%d",
                        geometry, ratio, enabled)
        except Exception:
            logger.debug("VKEYBOARD geometry diagnostics unavailable", exc_info=True)

    def _capture_keyboard_target(self):
        """Remember an actually foreground external HWND; retain it during dashboard activation."""
        try:
            api = getattr(self.action_adapter, "api", None)
            if api is None or not callable(getattr(api, "foreground", None)):
                logger.debug("VKEYBOARD target_capture_skipped reason=foreground_api_unavailable")
                return False
            target = api.foreground()
            if (target and target.get("hwnd") not in (None, 0)
                    and target.get("pid") != self.action_adapter.own_pid):
                self._keyboard_target = dict(target)
                logger.info("VKEYBOARD target_captured hwnd=%s pid=%s exe=%s",
                            target.get("hwnd"), target.get("pid"), target.get("exe", ""))
                return True
            if self._keyboard_target is not None:
                logger.info("VKEYBOARD target_preserved hwnd=%s reason=dashboard_foreground",
                            self._keyboard_target.get("hwnd"))
                return True
        except Exception as exc:
            logger.warning("VKEYBOARD target_capture_failed error_type=%s", type(exc).__name__)
        self._keyboard_target = None
        logger.warning("VKEYBOARD target_capture_failed reason=no_external_foreground_window")
        return False

    def _on_application_state_changed(self, _state):
        """Record a real external foreground window when SynapseOS deactivates."""
        self._capture_keyboard_target()

    def _dispatch_virtual_keyboard_input(self, method, *args, **kwargs):
        """Restore and verify the app selected before the virtual keyboard opened."""
        target = self._keyboard_target
        if target is None:
            raise RuntimeError(
                "No typing target was captured. Bring the intended application to the foreground once, "
                "then return to SynapseOS and reopen the virtual keyboard."
            )

        def send_to_target():
            api = self.action_adapter.api
            live_target = next((window for window in api.windows()
                                if window.get("hwnd") == target.get("hwnd")
                                and window.get("pid") == target.get("pid")
                                and window.get("pid") != self.action_adapter.own_pid), None)
            if live_target is None:
                raise RuntimeError("The selected target application is no longer available.")
            foreground = api.foreground()
            if not foreground or foreground.get("hwnd") != live_target.get("hwnd"):
                if not api.activate(live_target):
                    raise RuntimeError("Could not restore the selected target application.")
            foreground = api.foreground()
            if not foreground or foreground.get("hwnd") != live_target.get("hwnd"):
                raise RuntimeError("The selected target application did not receive foreground focus.")
            logger.info("VKEYBOARD input_dispatch method=%s target_hwnd=%s target_pid=%s",
                        method, live_target.get("hwnd"), live_target.get("pid"))
            return self.action_adapter.keyboard_input_in_transaction(method, *args, **kwargs)

        return self.input_coordinator.run_voice_transaction(send_to_target)

    def request_voice_action(self, intent):
        if QThread.currentThread() == self.thread():
            return self.execute_voice_action(intent)
        request = VoiceActionRequest(intent)
        with self._voice_action_lock:
            self._voice_action_requests.add(request)
        try:
            self.bridge.voice_action.emit(request)
            if request.wait(30):
                return {"status": "cancelled", "message": "Action expired before the dashboard could start it."}
            if "exception" in request.result: raise request.result["exception"]
            return request.result.get("value")
        finally:
            with self._voice_action_lock:
                self._voice_action_requests.discard(request)

    @pyqtSlot(object)
    def _run_queued_voice_action(self, payload):
        request = payload
        if not request.claim():
            return
        ui_intents = {
            "app.open_keyboard", "voice.start_eye", "voice.pause_eye", "voice.resume_eye",
            "voice.stop_eye", "voice.calibrate_gaze", "voice.stop_control",
            "system.emergency_stop", "assistant.quit",
        }
        if request.intent.name in ui_intents:
            self._complete_voice_action(request)
            return
        try:
            threading.Thread(target=self._complete_voice_action, args=(request,),
                             name="SynapseVoiceAction", daemon=True).start()
        except Exception as exc:
            request.complete(exception=exc)

    def _complete_voice_action(self, request):
        try:
            value = self.execute_voice_action(request.intent)
        except Exception as exc:
            request.complete(exception=exc)
        else:
            request.complete(value=value)

    def execute_voice_action(self, intent):
        command = intent.name
        if self.emergency and command != "system.emergency_stop": raise RuntimeError("Emergency stop is active")
        actions = {
            "app.open_keyboard": self.open_keyboard,
            "voice.start_eye": lambda: self.start_eye(voice_requested=True),
            "voice.pause_eye": lambda: self.pause_eye(voice_requested=True),
            "voice.resume_eye": lambda: self.resume_eye(voice_requested=True),
            "voice.stop_eye": lambda: self.stop_eye(voice_requested=True),
            "voice.calibrate_gaze": lambda: self.calibrate(voice_requested=True),
            "voice.stop_control": self.pause_voice,
            "system.emergency_stop": lambda: self.emergency_stop(voice_requested=True),
            "assistant.quit": lambda: self.voice.stop(wait=False),
        }
        action = actions.get(command)
        if action:
            logger.info("ADAPTER selected_action=%s route=dashboard_callback", command)
            result = action()
            if isinstance(result, dict) and result.get("status") in {
                    "executed", "action_requested", "failed", "execution_failed", "cancelled", "unsupported"}:
                return result
            if result is False:
                return {"status": "failed", "message": "The dashboard action was rejected."}
            return {"status": "executed", "message": "Done."}
        if command == "accessibility.read_selection":
            logger.info("ADAPTER selected_action=%s route=clipboard_selection", command)
            old_text = self._clipboard_request("read")
            try:
                self.input_coordinator.run_voice_transaction(
                    lambda: self._coordinated_hotkey("ctrl", "c")
                )
                selected = self._clipboard_request("read")
            finally:
                self._clipboard_request("write", old_text)
            return {"status": "executed", "message": selected or "No text is selected."}
        logger.info("ADAPTER selected_action=%s route=WindowsControlAdapter.execute", command)
        return self.action_adapter.execute(intent)

    def _coordinated_hotkey(self, *keys):
        self.input_coordinator.check_voice_action()
        self.action_adapter.desktop.hotkey(*keys)

    def _clipboard_request(self, operation, value=None):
        if QThread.currentThread() == self.thread():
            clipboard = QApplication.clipboard()
            if operation == "read":
                return clipboard.text()
            clipboard.setText(value)
            return None
        request = _ClipboardRequest(operation, value)
        self.bridge.clipboard_request.emit(request)
        if not request.done.wait(5):
            raise TimeoutError("Clipboard request did not reach the dashboard thread.")
        if request.error is not None:
            raise request.error
        return request.result

    @pyqtSlot(object)
    def _run_clipboard_request(self, request):
        try:
            clipboard = QApplication.clipboard()
            if request.operation == "read":
                request.result = clipboard.text()
            elif request.operation == "write":
                clipboard.setText(request.value)
            else:
                raise ValueError("Unknown clipboard operation")
        except Exception as exc:
            request.error = exc
        finally:
            request.done.set()

    def keyboard_type(self, text):
        if self.keyboard is None:
            self.open_keyboard()
        return self.keyboard.type_text(text)

    def show_settings(self):
        self.show_status("Privacy: camera and microphone run only while started; emergency stop releases both")

    def refresh_status(self):
        if self.camera and self.camera.thread and not self.camera.thread.is_alive():
            self._camera_connected = False
            self.camera = None
            self._camera_calibrating = False
            if self._pending_voice_eye_stop:
                self._pending_voice_eye_stop = False
                self._voice_feedback("Eye tracking stopped.")
            final_status = self._camera_terminal_status or "Stopped"
            self.status.setText(final_status)
            self._update_eye_buttons()
            self._refresh_service_indicators()
            if self._shutdown_pending:
                self._shutdown_pending = False
                self.close()
            return
        if self.camera and self._camera_stop_requested and self.camera.thread and self.camera.thread.is_alive():
            self.status.setText("Camera worker is still stopping")
        self._update_eye_buttons()
        self._refresh_service_indicators()

    def emergency_stop(self, voice_requested=False):
        self.emergency = True
        self._voice_state = "Emergency stopped"
        self.input_coordinator.activate_emergency()
        self.cancel_voice_actions()
        self.voice.emergency_stop(wait=False)
        if self.camera:
            self._camera_stop_requested = True
            stopped = self.camera.stop(timeout=0)
            if stopped:
                self.camera = None
                self._camera_connected = False
                self._camera_terminal_status = "Stopped"
                self._pending_voice_eye_stop = False
            elif voice_requested:
                self._pending_voice_eye_stop = True
        if self.keyboard:
            self.keyboard.close()
            self.keyboard = None
        self.preview.setPixmap(QPixmap())
        self.preview.setText("Emergency stop active")
        if self.camera is None:
            self.show_status("All controls stopped")
            result = {"status": "executed", "message": "Emergency stop active; voice and eye controls are stopped."}
        else:
            self.show_status("Emergency stop active · camera worker is still stopping")
            result = {"status": "action_requested", "message": "Emergency stop active; camera worker is still stopping."}
        return result if voice_requested else bool(self.camera is None)

    def closeEvent(self, event):
        self.voice.stop()
        if self.camera:
            self.input_coordinator.pause_eye()
            if not self.camera.stop(timeout=0):
                self._shutdown_pending = True
                self._camera_stop_requested = True
                self.show_status("Camera worker is still stopping")
                event.ignore()
                return
            self.camera = None
        if self.keyboard:
            self.keyboard.close()
            self.keyboard = None
        event.accept()


if __name__ == "__main__":
    pyautogui.FAILSAFE = True
    application = QApplication(sys.argv)
    window = Window()
    window.show()
    sys.exit(application.exec_())
