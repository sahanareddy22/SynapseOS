"""Hardware-free dashboard integration tests for eye tracking controls."""
import importlib
import sys
import types
import unittest
from unittest.mock import Mock, patch
import numpy as np
from voice.catalog import parse_transcript


class BoundSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in tuple(self.callbacks):
            callback(*args)


class SignalDescriptor:
    def __set_name__(self, owner, name):
        self.name = "_signal_" + name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        signal = instance.__dict__.get(self.name)
        if signal is None:
            signal = instance.__dict__[self.name] = BoundSignal()
        return signal


class FakeWidget:
    def __init__(self, *_args, **_kwargs):
        self.enabled = True
        self._text = _args[0] if _args and isinstance(_args[0], str) else ""
        self.clicked = BoundSignal()

    def setEnabled(self, enabled): self.enabled = enabled
    def setObjectName(self, *_args): pass
    def setAlignment(self, *_args): pass
    def setMinimumSize(self, *_args): pass
    def setPixmap(self, pixmap): self.pixmap = pixmap
    def frameGeometry(self): return types.SimpleNamespace(x=lambda: 0, y=lambda: 0,
                                                           width=lambda: 980, height=lambda: 360)
    def devicePixelRatioF(self): return 1.0
    def isEnabled(self): return self.enabled
    def setText(self, text): self._text = text
    def text(self): return self._text
    def size(self): return types.SimpleNamespace()
    def addWidget(self, *_args): pass
    def addLayout(self, *_args): pass
    def addStretch(self, *_args): pass
    def addSpacing(self, *_args): pass
    def setStyleSheet(self, *_args): pass
    def setAlternatingRowColors(self, *_args): pass
    def setContentsMargins(self, *_args): pass
    def setSpacing(self, *_args): pass
    def setFixedWidth(self, *_args): pass
    def setProperty(self, name, value): setattr(self, name, value)
    def style(self): return types.SimpleNamespace(unpolish=lambda *_a: None, polish=lambda *_a: None)
    def resize(self, *_args): pass
    def setWindowTitle(self, *_args): pass
    def setCentralWidget(self, *_args): pass
    def show(self): pass
    def hide(self): self.hidden = True
    def raise_(self): pass


class FakeList(FakeWidget):
    def __init__(self, *_args, **_kwargs):
        super().__init__()
        self.items = []
        self.scrolled_to_bottom = False
    def addItem(self, item): self.items.append(item)
    def setWordWrap(self, *_args): pass
    def setUniformItemSizes(self, *_args): pass
    def scrollToBottom(self): self.scrolled_to_bottom = True


class FakeStack(FakeWidget):
    def __init__(self, *_args, **_kwargs):
        super().__init__()
        self.widgets = []
        self.current = None
    def addWidget(self, widget): self.widgets.append(widget)
    def setCurrentWidget(self, widget): self.current = widget
    def currentWidget(self): return self.current


class FakeMainWindow(FakeWidget):
    def close(self):
        event = FakeCloseEvent()
        self.closeEvent(event)
        self.last_close_event = event


class FakeCloseEvent:
    def __init__(self): self.accepted = False; self.ignored = False
    def accept(self): self.accepted = True
    def ignore(self): self.ignored = True


class FakeThread:
    def __init__(self): self.alive = False
    def is_alive(self): return self.alive


class FakePipeline:
    instances = []
    startup_status = None
    startup_success = True
    stop_success = True
    calibration_success = True

    def __init__(self, *args, **kwargs):
        self.on_frame = kwargs.get("on_frame")
        self.on_status = kwargs.get("on_status")
        self.controller_factory = kwargs.get("controller_factory")
        self.running = False
        self.thread = FakeThread()
        self.eye_controller = types.SimpleNamespace(
            pause_tracking=Mock(return_value=True), resume_tracking=Mock(return_value=True),
        )
        self.stop_calls = []
        self.calibrate_calls = 0
        type(self).instances.append(self)

    def start(self):
        if type(self).startup_status:
            self.on_status(type(self).startup_status)
        if type(self).startup_success and type(self).startup_status is None:
            self.running = True
            self.thread.alive = True
        else:
            self.running = False
            self.thread.alive = False
        return type(self).startup_success

    def stop(self, timeout=None):
        self.stop_calls.append(timeout)
        self.running = False
        if type(self).stop_success:
            self.thread.alive = False
            return True
        self.thread.alive = True
        return False

    def calibrate(self):
        self.calibrate_calls += 1
        return type(self).calibration_success

    def finish(self):
        self.running = False
        self.thread.alive = False
        self.on_status("Camera stopped")


def load_app():
    qt_core = types.ModuleType("PyQt5.QtCore")
    qt_core.Qt = types.SimpleNamespace(AlignCenter=0, KeepAspectRatio=0, SmoothTransformation=0,
                                       Window=1, WindowStaysOnTopHint=2,
                                       WindowDoesNotAcceptFocus=4, NoFocus=8)
    qt_core.QObject = type("QObject", (), {})
    qt_core.QMainWindow = FakeMainWindow
    qt_core.QThread = types.SimpleNamespace(currentThread=lambda: None)
    qt_core.QTimer = type("QTimer", (FakeWidget,), {"__init__": lambda self, *_a: setattr(self, "timeout", BoundSignal()), "start": lambda self, *_a: None})
    qt_core.pyqtSignal = lambda *_args: SignalDescriptor()
    qt_core.pyqtSlot = lambda *_args, **_kwargs: (lambda fn: fn)

    qt_gui = types.ModuleType("PyQt5.QtGui")
    class FakeQImage:
        Format_RGB888 = 0
        def __init__(self, data, width, height, stride, _format):
            self.data = data
            self.width = width
            self.height = height
            self.stride = stride
        def copy(self):
            return types.SimpleNamespace(
                data=bytes(self.data), width=self.width,
                height=self.height, stride=self.stride,
            )
    qt_gui.QImage = FakeQImage
    class FakePixmap:
        def __init__(self, image): self.image = image
        def scaled(self, *_args): return self
    qt_gui.QPixmap = type("QPixmap", (), {"fromImage": staticmethod(FakePixmap)})
    qt_gui.QColor = lambda value: value
    qt_widgets = types.ModuleType("PyQt5.QtWidgets")
    qt_widgets.QApplication = type("QApplication", (), {"clipboard": staticmethod(lambda: None)})
    qt_widgets.QMainWindow = FakeMainWindow
    for name in ("QGridLayout", "QHBoxLayout", "QLabel", "QPushButton", "QVBoxLayout", "QWidget"):
        setattr(qt_widgets, name, FakeWidget)
    qt_widgets.QStackedWidget = FakeStack
    qt_widgets.QListWidget = FakeList
    class FakeListItem:
        def __init__(self, text): self._text = text
        def setBackground(self, value): self.background = value
        def setForeground(self, value): self.foreground = value
        def __str__(self): return self._text
    qt_widgets.QListWidgetItem = FakeListItem
    pyqt = types.ModuleType("PyQt5")
    pyqt.QtCore, pyqt.QtGui, pyqt.QtWidgets = qt_core, qt_gui, qt_widgets

    cv2 = types.ModuleType("cv2")
    cv2.COLOR_BGR2RGB = 1
    cv2.cvtColor = Mock(side_effect=lambda frame, _code: frame)
    pyautogui = types.ModuleType("pyautogui")
    vision_pipeline = types.ModuleType("vision.camera_pipeline")
    vision_pipeline.CameraPipeline = FakePipeline
    vision_eye = types.ModuleType("vision.eye_control")
    vision_eye.EyeController = Mock()
    voice_assistant = types.ModuleType("voice.assistant")
    voice_state = {"running": False, "listening_enabled": False, "emergency_stopped": False}
    voice_assistant.VoiceAssistant = Mock(return_value=types.SimpleNamespace(
        start=Mock(), stop=Mock(), emergency_stop=Mock(), _feedback=Mock(), dispatcher=types.SimpleNamespace(
            listening_enabled=False, emergency_stopped=False, cancel_all=Mock()),
        status=Mock(side_effect=lambda: dict(voice_state)),
    ))
    voice_request = types.ModuleType("voice.gui_request")
    voice_request.VoiceActionRequest = Mock()
    adapter_module = types.ModuleType("voice.windows_adapter")
    adapter_module.WindowsControlAdapter = Mock(return_value=types.SimpleNamespace(cancel_all=Mock()))
    keyboard_module = types.ModuleType("vkeyboard")
    keyboard_module.VirtualKeyboard = Mock()

    modules = {
        "PyQt5": pyqt, "PyQt5.QtCore": qt_core, "PyQt5.QtGui": qt_gui,
        "PyQt5.QtWidgets": qt_widgets, "cv2": cv2, "pyautogui": pyautogui,
        "vision.camera_pipeline": vision_pipeline, "vision.eye_control": vision_eye,
        "voice.assistant": voice_assistant, "voice.gui_request": voice_request,
        "voice.windows_adapter": adapter_module, "vkeyboard": keyboard_module,
    }
    with patch.dict(sys.modules, modules):
        sys.modules.pop("app", None)
        module = importlib.import_module("app")
    return module


app_module = load_app()


class DashboardEyeTests(unittest.TestCase):
    def setUp(self):
        app_module.DEFAULT_INPUT_COORDINATOR.reset_emergency(confirmed=True)
        app_module.DEFAULT_INPUT_COORDINATOR.resume_eye()
        FakePipeline.instances.clear()
        FakePipeline.startup_status = None
        FakePipeline.startup_success = True
        FakePipeline.stop_success = True
        FakePipeline.calibration_success = True
        app_module.VirtualKeyboard.reset_mock()
        self.window = app_module.Window()

    def test_sidebar_changes_pages_without_recreating_or_stopping_services(self):
        self.window.start_eye()
        pipeline = self.window.camera
        self.window.start_voice()
        voice = self.window.voice
        stop_calls_before = voice.stop.call_count
        for page_id in ("eye", "voice", "activity", "settings", "dashboard"):
            self.assertTrue(self.window.navigate_to(page_id))
            self.assertIs(self.window.page_stack.currentWidget(), self.window.pages[page_id])
        self.assertIs(self.window.camera, pipeline)
        self.assertIs(self.window.voice, voice)
        self.assertEqual(len(FakePipeline.instances), 1)
        self.assertEqual(pipeline.stop_calls, [])
        self.assertEqual(voice.stop.call_count, stop_calls_before)

    def test_camera_and_voice_status_indicators_are_independent(self):
        self.window.show_status("Camera connected")
        self.assertEqual(self.window.camera_status.text(), "Camera · Connected")
        self.assertEqual(self.window.voice_status.text(), "Voice · Stopped")
        self.window.show_status("Voice listener starting.")
        self.assertEqual(self.window.camera_status.text(), "Camera · Connected")
        self.assertEqual(self.window.voice_status.text(), "Voice · Starting")

    def test_emergency_stop_is_in_persistent_shell_and_uses_existing_handler(self):
        self.assertIsNotNone(self.window.emergency_button)
        self.assertIn(self.window.emergency_stop, self.window.emergency_button.clicked.callbacks)

    def test_voice_console_renders_recognized_user_command_and_assistant_response(self):
        self.window.bridge.voice_event.emit("Heard: open Notepad")
        self.window.bridge.voice_event.emit("Notepad opened.")
        self.assertIn("YOU · RECOGNIZED", str(self.window.voice_events.items[0]))
        self.assertIn("open Notepad", str(self.window.voice_events.items[0]))
        self.assertIn("ASSISTANT · STATUS / RESPONSE", str(self.window.voice_events.items[1]))
        self.assertIn("Notepad opened.", str(self.window.voice_events.items[1]))
        self.assertEqual(self.window.voice_events.items[0].background, "#E9EEE5")
        self.assertEqual(self.window.voice_events.items[1].background, "#FFFFFF")
        self.assertTrue(self.window.voice_empty_state.hidden)
        self.assertTrue(self.window.voice_events.scrolled_to_bottom)

    def test_voice_console_empty_state_and_catalog_examples(self):
        self.assertFalse(getattr(self.window.voice_empty_state, "hidden", False))
        self.assertGreater(len(self.window.voice_command_examples.items), 40)
        self.assertTrue(any("browser.tab.new" in item for item in self.window.voice_command_examples.items))
        self.assertFalse(any("app.target." in item for item in self.window.voice_command_examples.items))

    def test_voice_controls_keep_existing_handlers_and_events_use_qt_bridge(self):
        self.assertIn(self.window.start_voice, self.window.voice_start_button.clicked.callbacks)
        self.assertIn(self.window.stop_voice, self.window.voice_stop_button.clicked.callbacks)
        self.assertIn(self.window._on_voice_event, self.window.bridge.voice_event.callbacks)
        self.window.voice_start_button.clicked.emit()
        self.window.voice_stop_button.clicked.emit()
        self.window.voice.start.assert_called()
        self.window.voice.stop.assert_called()

    def test_multiple_voice_events_remain_in_emission_order(self):
        messages = ("Heard: first command", "First response", "Heard: second command", "Second response")
        for message in messages:
            self.window.bridge.voice_event.emit(message)
        rendered = [str(item) for item in self.window.voice_events.items]
        self.assertEqual(len(rendered), len(messages))
        self.assertIn("first command", rendered[0])
        self.assertIn("First response", rendered[1])
        self.assertIn("second command", rendered[2])
        self.assertIn("Second response", rendered[3])
        self.assertTrue(self.window.voice_events.scrolled_to_bottom)

    def test_long_voice_message_is_rendered_without_truncation(self):
        long_text = "recognized phrase " * 300
        self.window.bridge.voice_event.emit("Heard: " + long_text)
        self.assertTrue(self.window.voice_events.items[0]._text.endswith(long_text))

    def test_open_keyboard_shows_and_raises_same_window_without_closing_it(self):
        self.window.open_keyboard()
        keyboard = self.window.keyboard
        self.window.open_keyboard()

        keyboard.show.assert_called_with()
        keyboard.raise_.assert_called_with()
        app_module.VirtualKeyboard.assert_called_once()
        keyboard.close.assert_not_called()

    def test_keyboard_types_into_captured_foreground_target_and_restores_focus(self):
        target = {"hwnd": 101, "pid": 501, "exe": "notepad.exe", "title": "Untitled - Notepad"}
        dashboard = {"hwnd": 202, "pid": 99, "exe": "python.exe", "title": "SynapseOS"}
        class Api:
            active = target
            def foreground(self): return self.active
            def windows(self): return [target, dashboard]
            def activate(self, window):
                self.active = window
                return True
        api = Api()
        input_adapter = Mock()
        self.window.action_adapter = types.SimpleNamespace(
            api=api, own_pid=99, keyboard_input_in_transaction=input_adapter,
        )
        self.window.open_keyboard()
        self.assertEqual(self.window._keyboard_target["hwnd"], target["hwnd"])

        api.active = dashboard
        self.window._dispatch_virtual_keyboard_input("write", "h")

        self.assertIs(api.active, target)
        input_adapter.assert_called_once_with("write", "h")
        self.window.keyboard.show.assert_called_once()
        self.window.keyboard.close.assert_not_called()

    def test_dashboard_activation_preserves_last_external_foreground_handle(self):
        target = {"hwnd": 101, "pid": 501, "exe": "notepad.exe", "title": "Notepad"}
        dashboard = {"hwnd": 202, "pid": 99, "exe": "python.exe", "title": "Notepad"}
        class Api:
            active = target
            def foreground(self): return self.active
        api = Api()
        self.window.action_adapter = types.SimpleNamespace(api=api, own_pid=99)

        self.assertTrue(self.window._capture_keyboard_target())
        api.active = dashboard
        self.window._on_application_state_changed(None)
        self.window.open_keyboard()

        self.assertEqual(self.window._keyboard_target["hwnd"], 101)
        self.assertEqual(self.window._keyboard_target["pid"], 501)

    def test_notepad_title_on_dashboard_is_not_mistaken_for_target(self):
        dashboard = {"hwnd": 202, "pid": 99, "exe": "python.exe", "title": "Notepad"}
        self.window.action_adapter = types.SimpleNamespace(
            api=types.SimpleNamespace(foreground=lambda: dashboard), own_pid=99,
        )
        self.assertFalse(self.window._capture_keyboard_target())
        self.assertIsNone(self.window._keyboard_target)

    def test_closed_retained_target_is_rejected_without_sending_input(self):
        target = {"hwnd": 101, "pid": 501, "exe": "notepad.exe"}
        input_adapter = Mock()
        api = types.SimpleNamespace(
            foreground=lambda: None,
            windows=lambda: [],
            activate=Mock(return_value=True),
        )
        self.window.action_adapter = types.SimpleNamespace(
            api=api, own_pid=99, keyboard_input_in_transaction=input_adapter,
        )
        self.window._keyboard_target = target

        with self.assertRaisesRegex(RuntimeError, "no longer available"):
            self.window._dispatch_virtual_keyboard_input("write", "x")
        input_adapter.assert_not_called()

    def test_reused_window_handle_with_different_process_is_rejected(self):
        target = {"hwnd": 101, "pid": 501, "exe": "notepad.exe"}
        replacement = {"hwnd": 101, "pid": 777, "exe": "other.exe"}
        input_adapter = Mock()
        api = types.SimpleNamespace(
            foreground=lambda: replacement,
            windows=lambda: [replacement],
            activate=Mock(return_value=True),
        )
        self.window.action_adapter = types.SimpleNamespace(
            api=api, own_pid=99, keyboard_input_in_transaction=input_adapter,
        )
        self.window._keyboard_target = target

        with self.assertRaisesRegex(RuntimeError, "no longer available"):
            self.window._dispatch_virtual_keyboard_input("write", "x")
        api.activate.assert_not_called()
        input_adapter.assert_not_called()

    def test_keyboard_input_fails_closed_without_external_target_or_during_emergency(self):
        target = {"hwnd": 101, "pid": 501, "exe": "notepad.exe"}
        class Api:
            active = None
            def foreground(self): return self.active
            def windows(self): return [target] if self.active else []
            def activate(self, window):
                self.active = window
                return True
        api = Api()
        input_adapter = Mock()
        self.window.action_adapter = types.SimpleNamespace(
            api=api, own_pid=99, keyboard_input_in_transaction=input_adapter,
        )
        self.window.open_keyboard()
        with self.assertRaisesRegex(RuntimeError, "No typing target was captured"):
            self.window._dispatch_virtual_keyboard_input("write", "x")
        input_adapter.assert_not_called()

        api.active = target
        self.window._keyboard_target = target
        self.window.input_coordinator.activate_emergency()
        with self.assertRaises(Exception):
            self.window._dispatch_virtual_keyboard_input("write", "x")
        input_adapter.assert_not_called()

    def test_preview_conversion_keeps_rgb_qimage_memory_alive(self):
        frame = np.zeros((5, 7, 3), dtype=np.uint8)
        frame[0, 0] = (10, 20, 30)

        self.window.show_frame(frame)
        frame[:] = 255
        image = self.window.preview.pixmap.image

        self.assertEqual((image.width, image.height, image.stride), (7, 5, 21))
        self.assertEqual(image.data[:3], bytes((30, 20, 10)))

    def test_preview_conversion_failure_reports_error_without_raising(self):
        with patch.object(app_module, "validate_canonical_bgr_frame", side_effect=ValueError("bad preview")):
            self.window.show_frame(np.zeros((5, 7, 3), dtype=np.uint8))

        self.assertIn("Camera preview error", self.window.status.text())

    def test_new_eye_voice_aliases_are_explicit_and_existing_stop_voice_meaning_is_kept(self):
        expected = {
            "pause eye tracking": "voice.pause_eye",
            "pause eye control": "voice.pause_eye",
            "resume eye tracking": "voice.resume_eye",
            "resume eye control": "voice.resume_eye",
            "stop eye tracking": "voice.stop_eye",
            "start eye tracking": "voice.start_eye",
            "calibrate gaze": "voice.calibrate_gaze",
            "stop voice control": "voice.stop_control",
            "emergency stop": "system.emergency_stop",
            "stop everything": "system.emergency_stop",
        }
        for phrase, intent in expected.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(parse_transcript(phrase).name, intent)

    def test_start_success_and_repeated_start_do_not_create_workers(self):
        self.window.start_eye()
        self.window.start_eye()
        self.assertEqual(len(FakePipeline.instances), 1)
        self.assertTrue(FakePipeline.instances[0].thread.alive)
        self.assertFalse(self.window.start_eye_button.enabled)
        self.assertTrue(self.window.stop_eye_button.enabled)
        self.assertIsNotNone(FakePipeline.instances[0].controller_factory)
        self.assertEqual(FakePipeline.instances[0].on_frame.__self__, self.window)

    def test_preview_updates_are_rate_limited_before_the_qt_signal(self):
        emit = Mock()
        self.window.bridge.frame.emit = emit
        frames = [object(), object(), object()]
        with patch.object(app_module.time, "monotonic", side_effect=[10.0, 10.01, 10.1]):
            for frame in frames:
                self.window._emit_camera_preview(frame)
        self.assertEqual([call.args[0] for call in emit.call_args_list], [frames[0], frames[2]])

    def test_camera_initialization_failure_is_shown_and_start_recovers(self):
        FakePipeline.startup_success = True
        FakePipeline.startup_status = "Camera unavailable; startup attempts exhausted"
        self.window.start_eye()
        self.assertEqual(self.window.status.text(), "Camera unavailable")
        self.assertTrue(self.window.start_eye_button.enabled)
        self.assertFalse(self.window.calibrate_button.enabled)

    def test_camera_connection_and_calibration_progress_update_controls_via_signal(self):
        self.window.start_eye()
        pipeline = FakePipeline.instances[0]
        pipeline.on_status("Camera connected")
        self.assertTrue(self.window.calibrate_button.enabled)
        self.assertTrue(self.window.calibrate())
        self.assertEqual(pipeline.calibrate_calls, 1)
        pipeline.on_status("Look at the screen center target and hold steady: sample 1/15")
        self.assertFalse(self.window.calibrate_button.enabled)
        pipeline.on_status("Center captured. Look at the left screen target, then activate Calibrate gaze.")
        self.assertTrue(self.window.calibrate_button.enabled)
        pipeline.on_status("Five-point gaze calibration complete; cursor control enabled.")
        self.assertEqual(self.window.status.text(), "Calibration completed")
        self.assertTrue(self.window.calibrate_button.enabled)

    def test_voice_eye_callbacks_report_results_and_keep_stop_voice_control_separate(self):
        self.window.start_eye()
        pipeline = FakePipeline.instances[0]
        pipeline.on_status("Camera connected")
        start_result = self.window.execute_voice_action(types.SimpleNamespace(name="voice.start_eye"))
        self.assertEqual(start_result["status"], "executed")
        self.window.voice.dispatcher.cancel_all.reset_mock()
        stop_voice = self.window.execute_voice_action(types.SimpleNamespace(name="voice.stop_control"))
        self.assertEqual(stop_voice["status"], "executed")
        self.assertEqual(pipeline.stop_calls, [])
        self.assertTrue(pipeline.running)

        FakePipeline.calibration_success = False
        pipeline.calibrate = Mock(return_value=False)
        calibration = self.window.execute_voice_action(types.SimpleNamespace(name="voice.calibrate_gaze"))
        self.assertEqual(calibration["status"], "failed")
        self.assertFalse(pipeline.stop_calls)

        paused = self.window.execute_voice_action(types.SimpleNamespace(name="voice.pause_eye"))
        resumed = self.window.execute_voice_action(types.SimpleNamespace(name="voice.resume_eye"))
        self.assertEqual(paused["status"], "executed")
        self.assertEqual(resumed["status"], "executed")
        self.assertTrue(pipeline.running)

    def test_voice_stop_eye_reports_timeout_and_preserves_pipeline_until_worker_exit(self):
        self.window.start_eye()
        pipeline = FakePipeline.instances[0]
        FakePipeline.stop_success = False
        result = self.window.execute_voice_action(types.SimpleNamespace(name="voice.stop_eye"))
        self.assertEqual(result["status"], "action_requested")
        self.assertIs(self.window.camera, pipeline)
        self.assertIn("still stopping", result["message"])
        pipeline.finish()
        self.window.refresh_status()
        self.assertIsNone(self.window.camera)

    def test_calibration_is_blocked_when_disconnected(self):
        self.window.start_eye()
        pipeline = FakePipeline.instances[0]
        self.assertFalse(self.window.calibrate())
        self.assertEqual(pipeline.calibrate_calls, 0)
        self.assertFalse(self.window.calibrate_button.enabled)

    def test_rejected_calibration_can_be_retried(self):
        self.window.start_eye()
        pipeline = FakePipeline.instances[0]
        pipeline.on_status("Camera connected")
        self.assertTrue(self.window.calibrate())
        self.assertFalse(self.window.calibrate_button.enabled)
        pipeline.on_status("Calibration needs a recent, visible, open-eye gaze sample")
        self.assertTrue(self.window.calibrate_button.enabled)

    def test_successful_stop_reports_stopped(self):
        self.window.start_eye()
        pipeline = FakePipeline.instances[0]
        self.window.stop_eye()
        self.assertEqual(pipeline.stop_calls, [0])
        self.assertIsNone(self.window.camera)
        self.assertEqual(self.window.status.text(), "Stopped")

    def test_stop_timeout_keeps_pipeline_and_blocks_restart_until_exit(self):
        self.window.start_eye()
        pipeline = FakePipeline.instances[0]
        FakePipeline.stop_success = False
        self.window.stop_eye()
        self.assertIs(self.window.camera, pipeline)
        self.assertEqual(self.window.status.text(), "Camera worker is still stopping")
        self.window.start_eye()
        self.assertEqual(len(FakePipeline.instances), 1)
        self.assertEqual(self.window.status.text(), "Camera worker is still stopping")
        pipeline.finish()
        self.window.refresh_status()
        self.assertEqual(self.window.status.text(), "Stopped")
        self.assertIsNone(self.window.camera)

    def test_shutdown_waits_for_active_camera_worker_exit(self):
        self.window.start_eye()
        pipeline = FakePipeline.instances[0]
        FakePipeline.stop_success = False
        event = FakeCloseEvent()
        self.window.closeEvent(event)
        self.assertTrue(event.ignored)
        self.assertTrue(self.window._shutdown_pending)
        pipeline.finish()
        self.window.refresh_status()
        self.assertTrue(self.window.last_close_event.accepted)
        self.assertFalse(self.window._shutdown_pending)

    def test_stop_timeout_status_and_camera_errors_flow_through_bridge_signal(self):
        self.window.start_eye()
        pipeline = FakePipeline.instances[0]
        pipeline.on_status("Camera error: RuntimeError")
        self.assertEqual(self.window.status.text(), "Camera error · RuntimeError")
        self.assertFalse(self.window.calibrate_button.enabled)


if __name__ == "__main__":
    unittest.main()
