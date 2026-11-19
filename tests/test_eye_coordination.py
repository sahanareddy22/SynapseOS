"""Eye pause/resume tests with mocked Face Mesh and PyAutoGUI."""
import types
import unittest
from unittest.mock import Mock

import numpy as np

from desktop_input import DesktopInputCoordinator
from tests.test_eye_control import FakeClock, GazeTests, eye_module, fake_pyautogui


class EyeCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.coordinator = DesktopInputCoordinator()
        self.actions = Mock()
        self.controller = eye_module.EyeController(
            clock=self.clock, action_dispatcher=self.actions,
            input_coordinator=self.coordinator,
        )
        self.frame = np.zeros((480, 640, 3), dtype=np.uint8)
        fake_pyautogui.moveTo.reset_mock()
        fake_pyautogui.click.reset_mock()
        fake_pyautogui.doubleClick.reset_mock()
        fake_pyautogui.mouseDown.reset_mock()
        fake_pyautogui.mouseUp.reset_mock()

    @staticmethod
    def close_eye(points, side, closed=True):
        pairs = ((386, 374), (387, 373)) if side == "left" else ((159, 145), (158, 153))
        gap = 0.005 if closed else 0.04
        for upper, lower in pairs:
            points[upper] = types.SimpleNamespace(x=points[upper].x, y=0.40 - gap / 2)
            points[lower] = types.SimpleNamespace(x=points[lower].x, y=0.40 + gap / 2)

    def process(self, at, points=None):
        self.clock.set(at)
        points = points if points is not None else GazeTests.landmarks()
        face = types.SimpleNamespace(landmark=points)
        self.controller.face_mesh.process.return_value = types.SimpleNamespace(multi_face_landmarks=[face])
        self.controller.process_frame(self.frame)

    def test_pause_blocks_gaze_and_blink_actions_but_keeps_face_mesh_processing(self):
        self.controller.calibration = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.5))
        self.controller.previous = (640, 360)
        self.assertTrue(self.controller.pause_tracking())
        self.process(0.1, GazeTests.landmarks(0.9, 0.0))
        self.assertTrue(self.controller.face_mesh.process.called)
        self.assertEqual(self.controller.calibration, ((1.0, 0.0, 0.0), (0.0, 1.0, 0.5)))
        self.assertFalse(fake_pyautogui.moveTo.called)
        self.assertFalse(self.coordinator.run_eye_action("click", lambda: fake_pyautogui.click()))
        fake_pyautogui.click.assert_not_called()

    def test_resume_preserves_calibration_and_arms_only_after_eyes_reopen(self):
        calibration = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.5))
        self.controller.calibration = calibration
        self.controller.pause_tracking()
        self.assertTrue(self.controller.resume_tracking())
        closed = GazeTests.landmarks()
        self.close_eye(closed, "left", True)
        self.process(0.1, closed)
        self.process(0.3)
        self.assertEqual(self.actions.call_count, 0)
        self.process(0.4, closed)
        self.process(0.5)
        self.assertEqual(self.actions.call_args_list[0].args, ("left",))
        self.assertEqual(self.controller.calibration, calibration)

    def test_emergency_cannot_be_cleared_by_pause_or_resume(self):
        self.controller.calibration = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.5))
        self.coordinator.activate_emergency()
        self.assertFalse(self.controller.pause_tracking())
        self.assertFalse(self.controller.resume_tracking())
        self.assertTrue(self.coordinator.emergency_active)
        self.assertIsNotNone(self.controller.calibration)

    def test_completed_blink_reaches_shared_coordinator_and_mouse_boundary(self):
        self.controller._action_dispatcher = self.controller._dispatch_blink_action
        closed = GazeTests.landmarks()
        self.close_eye(closed, "left", True)
        with self.assertLogs("vision.eye_control", level="INFO") as eye_logs:
            with self.assertLogs("desktop_input", level="INFO") as coordinator_logs:
                self.process(0.10, closed)
                self.process(0.20)

        fake_pyautogui.mouseDown.assert_called_once_with(button="left", _pause=False)
        fake_pyautogui.mouseUp.assert_called_once_with(button="left", _pause=False)
        self.assertIn("eye_closed side=left", " ".join(eye_logs.output))
        self.assertIn("eye_reopened side=left", " ".join(eye_logs.output))
        self.assertIn("click_transaction_accepted action=left", " ".join(eye_logs.output))
        self.assertIn("eye_action=accepted kind=click", " ".join(coordinator_logs.output))

    def test_emergency_stop_blocks_subsequent_blink_clicks(self):
        self.controller._action_dispatcher = self.controller._dispatch_blink_action
        self.coordinator.activate_emergency()
        closed = GazeTests.landmarks()
        self.close_eye(closed, "left", True)
        self.process(0.10, closed)
        self.process(0.20)

        fake_pyautogui.mouseDown.assert_not_called()
        fake_pyautogui.mouseUp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
