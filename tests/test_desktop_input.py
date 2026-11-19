"""Hardware-free tests for the shared voice/eye desktop input coordinator."""
import threading
import importlib
import sys
import time
import types
import unittest
from unittest.mock import Mock, patch

from desktop_input import DesktopInputBlocked, DesktopInputCoordinator
from voice.catalog import Intent

fake_pyautogui = types.ModuleType("pyautogui")
with patch.dict(sys.modules, {"pyautogui": fake_pyautogui}):
    sys.modules.pop("voice.windows_adapter", None)
    WindowsControlAdapter = importlib.import_module("voice.windows_adapter").WindowsControlAdapter


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = DesktopInputCoordinator()

    @staticmethod
    def wait_for(predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.002)
        return predicate()

    def test_voice_waits_for_current_eye_action_and_has_priority_over_new_eye_actions(self):
        eye_entered = threading.Event()
        release_eye = threading.Event()
        voice_entered = threading.Event()
        order = []

        def eye_action():
            order.append("eye-start")
            eye_entered.set()
            release_eye.wait(1)
            order.append("eye-end")

        eye_thread = threading.Thread(target=lambda: self.coordinator.run_eye_action("move", eye_action, (1, 1)))
        voice_thread = threading.Thread(target=lambda: self.coordinator.run_voice_transaction(
            lambda: (order.append("voice-start"), voice_entered.set(), order.append("voice-end"))))
        eye_thread.start()
        self.assertTrue(eye_entered.wait(1))
        voice_thread.start()
        self.assertTrue(self.wait_for(lambda: self.coordinator._voice_waiters == 1))
        self.assertFalse(self.coordinator.run_eye_action("click", lambda: order.append("dropped-click")))
        self.assertFalse(self.coordinator.run_eye_action("move", lambda: order.append("stale-move"), (2, 2)))
        self.assertFalse(self.coordinator.run_eye_action("move", lambda: order.append("stale-move"), (3, 3)))
        self.assertEqual(self.coordinator.pending_gaze_target, (3, 3))
        self.assertFalse(voice_entered.is_set())
        release_eye.set()
        eye_thread.join(1)
        voice_thread.join(1)
        self.assertEqual(order, ["eye-start", "eye-end", "voice-start", "voice-end"])

    def test_gaze_updates_are_latest_only_and_fresh_frame_executes_after_voice(self):
        entered = threading.Event()
        release = threading.Event()
        voice = threading.Thread(target=lambda: self.coordinator.run_voice_transaction(
            lambda: (entered.set(), release.wait(1))))
        voice.start()
        self.assertTrue(entered.wait(1))
        calls = []
        for target in ((4, 5), (8, 9), (12, 13)):
            self.assertFalse(self.coordinator.run_eye_action("move", lambda t=target: calls.append(t), target))
        self.assertEqual(self.coordinator.pending_gaze_target, (12, 13))
        release.set()
        voice.join(1)
        self.assertTrue(self.coordinator.run_eye_action("move", lambda: calls.append((20, 21)), (20, 21)))
        self.assertEqual(calls, [(20, 21)])

    def test_emergency_latch_rejects_both_sources_and_requires_explicit_reset(self):
        self.coordinator.activate_emergency()
        self.assertFalse(self.coordinator.run_eye_action("click", Mock()))
        with self.assertRaises(DesktopInputBlocked):
            self.coordinator.run_voice_transaction(Mock())
        self.assertFalse(self.coordinator.resume_eye())
        self.assertFalse(self.coordinator.reset_emergency())
        self.assertTrue(self.coordinator.reset_emergency(confirmed=True))
        self.assertFalse(self.coordinator.eye_actions_enabled)
        self.assertTrue(self.coordinator.resume_eye())
        self.assertTrue(self.coordinator.run_eye_action("click", Mock()))

    def test_emergency_interrupts_multi_step_voice_transaction_at_next_boundary(self):
        started = threading.Event()
        continue_action = threading.Event()
        outcome = []

        def action():
            started.set()
            continue_action.wait(1)
            self.coordinator.check_voice_action()
            outcome.append("should-not-run")

        def run():
            try:
                self.coordinator.run_voice_transaction(action)
            except DesktopInputBlocked:
                outcome.append("cancelled")

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(started.wait(1))
        self.coordinator.activate_emergency()
        continue_action.set()
        worker.join(1)
        self.assertEqual(outcome, ["cancelled"])

    def test_completed_actions_are_not_silently_deduplicated(self):
        clicks = []
        self.assertTrue(self.coordinator.run_eye_action("click", lambda: clicks.append("first")))
        self.coordinator.run_voice_transaction(lambda: clicks.append("voice"))
        self.assertTrue(self.coordinator.run_eye_action("click", lambda: clicks.append("second")))
        self.assertEqual(clicks, ["first", "voice", "second"])

    def test_cancel_invalidates_waiting_voice_transaction(self):
        entered = threading.Event()
        release = threading.Event()
        eye = threading.Thread(target=lambda: self.coordinator.run_eye_action(
            "move", lambda: (entered.set(), release.wait(1)), (1, 1)))
        result = []

        def voice():
            try:
                self.coordinator.run_voice_transaction(lambda: result.append("executed"))
            except DesktopInputBlocked:
                result.append("cancelled")

        eye.start()
        self.assertTrue(entered.wait(1))
        voice_worker = threading.Thread(target=voice)
        voice_worker.start()
        self.assertTrue(self.wait_for(lambda: self.coordinator._voice_waiters == 1))
        self.coordinator.cancel_voice_actions()
        release.set()
        eye.join(1)
        voice_worker.join(1)
        self.assertEqual(result, ["cancelled"])


class AdapterCoordinationTests(unittest.TestCase):
    def test_drag_cleanup_releases_button_after_emergency_without_desktop_hardware(self):
        coordinator = DesktopInputCoordinator()
        desktop = types.SimpleNamespace(**{name: Mock() for name in (
            "mouseDown", "mouseUp", "position", "moveTo", "moveRel", "click", "doubleClick",
            "hotkey", "press", "write", "scroll", "hscroll", "size", "screenshot",
        )})
        desktop.position.return_value = (40, 50)
        api = types.SimpleNamespace(screen_size=lambda: (1920, 1080))
        adapter = WindowsControlAdapter(api=api, desktop=desktop, opener=Mock(), shell_run=Mock(),
                                        input_coordinator=coordinator, audio=Mock())
        adapter.execute(Intent("mouse.drag_start"))
        self.assertTrue(adapter.dragging)
        coordinator.activate_emergency()
        adapter.cancel_all()
        deadline = time.monotonic() + 1
        while desktop.mouseUp.call_count == 0 and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertEqual(desktop.mouseUp.call_count, 1)
        self.assertFalse(adapter.dragging)

    def test_typing_stops_between_characters_after_emergency(self):
        coordinator = DesktopInputCoordinator()
        written = []
        desktop = types.SimpleNamespace(**{name: Mock() for name in (
            "mouseDown", "mouseUp", "position", "moveTo", "moveRel", "click", "doubleClick",
            "hotkey", "press", "scroll", "hscroll", "size", "screenshot",
        )})

        def write(character, **_kwargs):
            written.append(character)
            if character == "a":
                coordinator.activate_emergency()

        desktop.write = Mock(side_effect=write)
        api = types.SimpleNamespace(screen_size=lambda: (1920, 1080))
        adapter = WindowsControlAdapter(api=api, desktop=desktop, opener=Mock(), shell_run=Mock(),
                                        input_coordinator=coordinator, audio=Mock())
        result = adapter.execute(Intent("keyboard.type_text", {"text": "abc"}))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(written, ["a"])


if __name__ == "__main__":
    unittest.main()
