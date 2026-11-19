"""Hardware-free blink state-machine regression tests."""
import importlib
import math
import sys
import types
import unittest
from unittest.mock import Mock, call, patch

import numpy as np


def load_eye_controller():
    face_mesh = types.SimpleNamespace(FaceMesh=Mock(return_value=Mock()))
    mediapipe = types.ModuleType("mediapipe")
    mediapipe.solutions = types.SimpleNamespace(face_mesh=face_mesh)

    pyautogui = types.ModuleType("pyautogui")
    pyautogui.size = Mock(return_value=(1280, 720))
    pyautogui.position = Mock(return_value=(640, 360))
    pyautogui.moveTo = Mock()
    pyautogui.click = Mock()
    pyautogui.doubleClick = Mock()
    pyautogui.mouseDown = Mock()
    pyautogui.mouseUp = Mock()

    with patch.dict(sys.modules, {"mediapipe": mediapipe, "pyautogui": pyautogui}):
        sys.modules.pop("vision.eye_control", None)
        module = importlib.import_module("vision.eye_control")
    return module, pyautogui


eye_module, fake_pyautogui = load_eye_controller()


class ForeignNumpyArray:
    def __init__(self, array):
        self.array = array

    @property
    def __array_interface__(self):
        return self.array.__array_interface__


ForeignNumpyArray.__name__ = "ndarray"
ForeignNumpyArray.__qualname__ = "ndarray"
ForeignNumpyArray.__module__ = "numpy"


class FakeClock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now
    def set(self, now): self.now = now


class FrameProcessingTests(unittest.TestCase):
    def setUp(self):
        self.controller = eye_module.EyeController()
        self.controller.face_mesh.process.reset_mock()
        self.controller.face_mesh.process.side_effect = None
        self.controller.face_mesh.process.return_value = types.SimpleNamespace(
            multi_face_landmarks=None
        )

    def tearDown(self):
        self.controller.close()

    def test_bgr_frame_is_passed_to_face_mesh_as_rgb_uint8_contiguous(self):
        frame = np.zeros((8, 12, 3), dtype=np.uint8)
        frame[0, 0] = (11, 22, 33)

        self.controller.process_frame(frame)

        rgb = self.controller.face_mesh.process.call_args.args[0]
        self.assertIs(type(rgb), np.ndarray)
        self.assertEqual(rgb.shape, (8, 12, 3))
        self.assertEqual(rgb.dtype, np.uint8)
        self.assertTrue(rgb.flags.c_contiguous)
        self.assertTrue(rgb.flags.writeable)
        self.assertEqual(tuple(rgb[0, 0]), (33, 22, 11))

    def test_foreign_numpy_ndarray_is_copied_before_face_mesh(self):
        bgr = np.zeros((7, 9, 3), dtype=np.uint8)
        bgr[0, 0] = (1, 2, 3)
        foreign = ForeignNumpyArray(bgr)

        self.controller.process_frame(foreign)

        rgb = self.controller.face_mesh.process.call_args.args[0]
        self.assertIs(type(rgb), np.ndarray)
        self.assertEqual(rgb.shape, (7, 9, 3))
        self.assertEqual(rgb.dtype, np.uint8)
        self.assertTrue(rgb.flags.c_contiguous)
        self.assertTrue(rgb.flags.writeable)
        self.assertEqual(tuple(rgb[0, 0]), (3, 2, 1))

    def test_non_contiguous_input_is_copied_to_contiguous_rgb(self):
        frame = np.zeros((8, 24, 3), dtype=np.uint8)[:, ::2, :]
        frame[1, 2] = (4, 5, 6)
        self.assertFalse(frame.flags.c_contiguous)

        self.controller.process_frame(frame)

        rgb = self.controller.face_mesh.process.call_args.args[0]
        self.assertEqual(rgb.shape, (8, 12, 3))
        self.assertEqual(rgb.dtype, np.uint8)
        self.assertTrue(rgb.flags.c_contiguous)
        self.assertEqual(tuple(rgb[1, 2]), (6, 5, 4))

    def test_invalid_shape_and_dtype_are_rejected_without_calling_mediapipe(self):
        invalid_frames = (
            None,
            np.zeros((8, 12), dtype=np.uint8),
            np.zeros((0, 12, 3), dtype=np.uint8),
            np.zeros((8, 12, 4), dtype=np.uint8),
            np.zeros((8, 12, 3), dtype=np.float32),
            object(),
        )
        for frame in invalid_frames:
            with self.subTest(frame_type=type(frame).__name__, shape=getattr(frame, "shape", None)):
                with self.assertRaises(eye_module.InvalidCameraFrame):
                    self.controller.process_frame(frame)
        self.controller.face_mesh.process.assert_not_called()

    def test_mediapipe_processing_exception_propagates_to_pipeline_boundary(self):
        self.controller.face_mesh.process.side_effect = TypeError("mock frame binding error")
        with self.assertRaisesRegex(eye_module.MediaPipeProcessingError, "mock frame binding error") as caught:
            self.controller.process_frame(np.zeros((8, 12, 3), dtype=np.uint8))
        self.assertEqual(caught.exception.pipeline_stage, "mediapipe_processing")
        self.assertIsInstance(caught.exception.__cause__, TypeError)

    def test_detected_landmarks_are_drawn_on_separate_preview_frame(self):
        frame = np.full((8, 12, 3), (11, 22, 33), dtype=np.uint8)
        landmarks = [types.SimpleNamespace(x=0.5, y=0.5)]
        self.controller.face_mesh.process.return_value = types.SimpleNamespace(
            multi_face_landmarks=[types.SimpleNamespace(landmark=landmarks)]
        )

        preview = self.controller.process_frame(frame)

        self.assertIsNot(preview, frame)
        self.assertEqual(tuple(frame[4, 6]), (11, 22, 33))
        self.assertEqual(tuple(preview[4, 6]), (40, 235, 170))
        rgb = self.controller.face_mesh.process.call_args.args[0]
        self.assertEqual(tuple(rgb[4, 6]), (33, 22, 11))
        self.assertNotEqual(tuple(preview[2, 6]), (11, 22, 33))

    def test_no_landmarks_returns_unannotated_frame(self):
        frame = np.zeros((8, 12, 3), dtype=np.uint8)

        preview = self.controller.process_frame(frame)

        self.assertIs(preview, frame)
        self.assertIs(type(preview), np.ndarray)
        self.assertTrue(preview.flags.c_contiguous and preview.flags.writeable)
        self.assertTrue(np.all(preview == 0))

    def test_invalid_landmark_coordinates_are_skipped_safely(self):
        frame = np.zeros((8, 12, 3), dtype=np.uint8)
        landmarks = [
            types.SimpleNamespace(x=float("nan"), y=0.5),
            types.SimpleNamespace(x=2.0, y=0.5),
            object(),
        ]
        preview = eye_module.EyeController._draw_face_landmarks(frame, landmarks)

        self.assertIsNot(preview, frame)
        self.assertTrue(np.all(preview == 0))

    def test_read_only_and_non_contiguous_frames_normalize_before_processing_and_drawing(self):
        frame = np.zeros((16, 24, 3), dtype=np.uint8)[:, ::2, :]
        frame.setflags(write=False)
        self.controller.face_mesh.process.return_value = types.SimpleNamespace(
            multi_face_landmarks=[types.SimpleNamespace(landmark=[
                types.SimpleNamespace(x=0.5, y=0.5)
            ])]
        )

        preview = self.controller.process_frame(frame)
        rgb = self.controller.face_mesh.process.call_args.args[0]

        self.assertEqual(preview.shape, frame.shape)
        self.assertTrue(preview.flags.c_contiguous and preview.flags.writeable)
        self.assertTrue(rgb.flags.c_contiguous and rgb.flags.writeable)
        self.assertTrue(preview.flags.c_contiguous and preview.flags.writeable)

    def test_landmark_drawing_failure_has_stage_and_preserves_original_error(self):
        original = RuntimeError("mock NumPy drawing failure")
        frame = np.zeros((8, 12, 3), dtype=np.uint8)
        with patch.object(eye_module.np, "clip", side_effect=original):
            with self.assertRaises(eye_module.LandmarkDrawingError) as caught:
                eye_module.EyeController._draw_face_landmarks(
                    frame, [types.SimpleNamespace(x=0.5, y=0.5)]
                )
        self.assertEqual(caught.exception.pipeline_stage, "landmark_drawing")
        self.assertIs(caught.exception.__cause__, original)


class BlinkTests(unittest.TestCase):
    def setUp(self):
        fake_pyautogui.click.reset_mock()
        fake_pyautogui.doubleClick.reset_mock()
        fake_pyautogui.mouseDown.reset_mock()
        fake_pyautogui.mouseUp.reset_mock()
        fake_pyautogui.moveTo.reset_mock()
        self.clock = FakeClock()
        self.actions = Mock()
        self.controller = eye_module.EyeController(
            clock=self.clock, action_dispatcher=self.actions,
        )
        self.landmarks = [types.SimpleNamespace(x=0.5, y=0.5) for _ in range(478)]
        # MediaPipe eye landmark groups retain their anatomical side after mirroring.
        self._set_eye("left", False)
        self._set_eye("right", False)

    def _set_y(self, index, value):
        self.landmarks[index] = types.SimpleNamespace(x=self.landmarks[index].x, y=value)

    def _set_eye(self, side, closed):
        if side == "left":
            corners = ((362, 0.60), (263, 0.70))
            pairs = ((386, 374), (387, 373))
        else:
            corners = ((33, 0.30), (133, 0.40))
            pairs = ((159, 145), (158, 153))
        for index, x in corners:
            self.landmarks[index] = types.SimpleNamespace(x=x, y=0.40)
        gap = 0.005 if closed else 0.04
        for upper, lower in pairs:
            x = self.landmarks[upper].x
            self.landmarks[upper] = types.SimpleNamespace(x=x, y=0.40 - gap / 2)
            x = self.landmarks[lower].x
            self.landmarks[lower] = types.SimpleNamespace(x=x, y=0.40 + gap / 2)

    def frame(self, at, left=False, right=False):
        self.clock.set(at)
        self._set_eye("left", left)
        self._set_eye("right", right)
        self.controller._process_blinks(self.landmarks, (640, 480), self.clock())

    def blink(self, side, start=0.10, end=0.20):
        self.frame(start, left=side == "left", right=side == "right")
        self.frame(end)

    def assert_actions(self, *expected):
        self.assertEqual([call.args[0] for call in self.actions.call_args_list], list(expected))
        fake_pyautogui.click.assert_not_called()
        fake_pyautogui.doubleClick.assert_not_called()

    def test_left_eye_blink_emits_one_left_click(self):
        self.blink("left")
        self.assert_actions("left")

    def test_anatomical_eye_landmark_groups_are_independent_when_frame_is_mirrored(self):
        left_points = [types.SimpleNamespace(x=0.5, y=0.5) for _ in range(478)]
        right_points = [types.SimpleNamespace(x=0.5, y=0.5) for _ in range(478)]
        self._set_eye("left", True)
        left_points[:] = self.landmarks
        self._set_eye("left", False)
        self._set_eye("right", True)
        right_points[:] = self.landmarks

        self.assertLess(eye_module.EyeController._eye_aspect_ratio(left_points, "left", (640, 480)),
                        eye_module.EyeController._eye_aspect_ratio(left_points, "right", (640, 480)))
        self.assertLess(eye_module.EyeController._eye_aspect_ratio(right_points, "right", (640, 480)),
                        eye_module.EyeController._eye_aspect_ratio(right_points, "left", (640, 480)))

    def test_right_eye_blink_emits_one_right_click(self):
        self.blink("right")
        self.assert_actions("right")

    def test_simultaneous_blink_emits_exactly_one_double_click(self):
        self.frame(0.10, left=True, right=True)
        self.frame(0.32)
        self.assert_actions("double")

    def test_left_then_right_are_separate_actions(self):
        self.blink("left", 0.10, 0.20)
        self.blink("right", 0.24, 0.34)
        self.assert_actions("left", "right")

    def test_right_then_left_are_separate_actions(self):
        self.blink("right", 0.10, 0.20)
        self.blink("left", 0.24, 0.34)
        self.assert_actions("right", "left")

    def test_two_quick_left_blinks_do_not_become_double_click(self):
        self.blink("left", 0.10, 0.20)
        self.blink("left", 0.26, 0.36)
        self.assert_actions("left", "left")

    def test_two_quick_right_blinks_do_not_become_double_click(self):
        self.blink("right", 0.10, 0.20)
        self.blink("right", 0.26, 0.36)
        self.assert_actions("right", "right")

    def test_subminimum_short_closure_is_ignored(self):
        self.blink("left", 0.10, 0.14)
        self.assert_actions()

    def test_long_closure_is_ignored(self):
        self.frame(0.10, left=True)
        self.frame(0.60, left=True)
        self.frame(0.80)
        self.assert_actions()

    def test_invalid_noisy_eye_landmark_cancels_candidate(self):
        self.frame(0.10, left=True)
        self.landmarks[145] = types.SimpleNamespace(x=0.3, y=float("nan"))
        self.frame(0.15, left=True)
        self.frame(0.22)
        self.assert_actions()

    def test_missing_eye_landmarks_cannot_trigger_click(self):
        self.frame(0.10, left=True)
        self.controller._process_blinks([], (640, 480), 0.15)
        self.frame(0.22)
        self.assert_actions()

    def test_repeated_frames_during_one_closure_produce_one_action(self):
        self.frame(0.10, left=True)
        self.frame(0.15, left=True)
        self.frame(0.20, left=True)
        self.frame(0.30)
        self.frame(0.35)
        self.assert_actions("left")

    def test_bilateral_action_waits_for_both_eyes_and_has_priority(self):
        self.frame(0.10, left=True, right=True)
        self.frame(0.32, right=True)
        self.assert_actions()
        self.frame(0.36)
        self.assert_actions("double")

    def test_short_natural_bilateral_closure_is_suppressed(self):
        self.frame(0.10, left=True, right=True)
        self.frame(0.22)
        self.assert_actions()

    def test_per_eye_cooldown_suppresses_jitter_retrigger(self):
        self.blink("left", 0.10, 0.20)
        self.blink("left", 0.23, 0.29)
        self.assert_actions("left")

    def test_default_pyautogui_action_boundary_is_mocked(self):
        self.controller._dispatch_blink_action("left")
        self.controller._dispatch_blink_action("right")
        self.controller._dispatch_blink_action("double")
        self.assertEqual(fake_pyautogui.mouseDown.call_args_list, [
            call(button="left", _pause=False),
            call(button="right", _pause=False),
            call(button="left", _pause=False),
            call(button="left", _pause=False),
        ])
        self.assertEqual(fake_pyautogui.mouseUp.call_args_list,
                         fake_pyautogui.mouseDown.call_args_list)

    def test_mouse_button_is_released_when_press_raises(self):
        fake_pyautogui.mouseDown.side_effect = RuntimeError("mock press failure")
        with self.assertRaisesRegex(RuntimeError, "mock press failure"):
            self.controller._dispatch_blink_action("left")
        fake_pyautogui.mouseUp.assert_called_once_with(button="left", _pause=False)
        fake_pyautogui.mouseDown.side_effect = None

    def test_emergency_during_double_click_releases_button_and_cancels_second_click(self):
        from desktop_input import DesktopInputCoordinator
        coordinator = DesktopInputCoordinator()
        self.controller.input_coordinator = coordinator
        fake_pyautogui.mouseDown.side_effect = lambda **_kwargs: coordinator.activate_emergency()

        self.controller._dispatch_blink_action("double")

        fake_pyautogui.mouseDown.assert_called_once_with(button="left", _pause=False)
        fake_pyautogui.mouseUp.assert_called_once_with(button="left", _pause=False)
        fake_pyautogui.mouseDown.side_effect = None

    def test_coordinator_rejection_suppresses_mouse_dispatch(self):
        from desktop_input import DesktopInputCoordinator
        coordinator = DesktopInputCoordinator()
        self.controller.input_coordinator = coordinator

        with self.assertLogs("desktop_input", level="INFO") as logs:
            result = coordinator.run_voice_transaction(
                lambda: self.controller._dispatch_blink_action("left")
            )

        self.assertFalse(result)
        fake_pyautogui.mouseDown.assert_not_called()
        fake_pyautogui.mouseUp.assert_not_called()
        self.assertIn("reason=voice_priority", " ".join(logs.output))

    def test_coordinator_rejection_suppresses_mouse_dispatch(self):
        from desktop_input import DesktopInputCoordinator
        coordinator = DesktopInputCoordinator()
        self.controller.input_coordinator = coordinator

        with self.assertLogs("desktop_input", level="INFO") as logs:
            result = coordinator.run_voice_transaction(
                lambda: self.controller._dispatch_blink_action("left")
            )

        self.assertFalse(result)
        fake_pyautogui.mouseDown.assert_not_called()
        fake_pyautogui.mouseUp.assert_not_called()
        self.assertIn("reason=voice_priority", " ".join(logs.output))


class GazeTests(unittest.TestCase):
    def setUp(self):
        fake_pyautogui.moveTo.reset_mock()
        fake_pyautogui.position.reset_mock()
        fake_pyautogui.size.return_value = (1280, 720)
        fake_pyautogui.position.return_value = (640, 360)
        self.clock = FakeClock()
        self.status = Mock()
        self.controller = eye_module.EyeController(clock=self.clock, on_status=self.status)
        self.frame = np.zeros((480, 640, 3), dtype=np.uint8)

    @staticmethod
    def landmarks(gaze_x=0.5, gaze_y=0.0, valid=True):
        points = [types.SimpleNamespace(x=0.5, y=0.5) for _ in range(478)]
        eyes = (
            ((33, 0.30), (133, 0.40), 468, ((159, 145), (158, 153))),
            ((362, 0.60), (263, 0.70), 473, ((386, 374), (387, 373))),
        )
        for first, second, iris_index, lid_pairs in eyes:
            for index, x in (first, second):
                points[index] = types.SimpleNamespace(x=x, y=0.40)
            for upper, lower in lid_pairs:
                points[upper] = types.SimpleNamespace(x=first[1] + 0.035, y=0.38)
                points[lower] = types.SimpleNamespace(x=first[1] + 0.035, y=0.42)
            x1, x2 = first[1], second[1]
            points[iris_index] = types.SimpleNamespace(
                x=x1 + gaze_x * (x2 - x1),
                y=0.40 + gaze_y * 0.1 * 640 / 480,
            )
        if not valid:
            points[468] = types.SimpleNamespace(x=float("nan"), y=0.4)
        return points

    def observe(self, gaze=(0.5, 0.0), at=None, landmarks=None):
        if at is not None:
            self.clock.set(at)
        points = landmarks if landmarks is not None else self.landmarks(*gaze)
        face = types.SimpleNamespace(landmark=points)
        self.controller.face_mesh.process.return_value = types.SimpleNamespace(multi_face_landmarks=[face])
        self.controller.process_frame(self.frame)

    def test_center_gaze_maps_near_primary_screen_center(self):
        self.controller.calibration = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.5))
        self.assertEqual(self.controller._map_gaze((0.5, 0.0)), (640, 360))

    def test_gaze_coordinates_use_primary_screen_size_not_preview_dimensions(self):
        self.controller.calibration = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.5))
        fake_pyautogui.size.return_value = (1920, 1080)
        self.assertEqual(self.controller._map_gaze((0.0, 0.0)), (0, 540))
        self.assertEqual(self.controller._map_gaze((1.0, 0.0)), (1919, 540))

    def test_left_and_right_gaze_map_in_consistent_horizontal_directions(self):
        self.controller.calibration = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.5))
        center = self.controller._map_gaze((0.5, 0.0))[0]
        self.assertLess(self.controller._map_gaze((0.1, 0.0))[0], center)
        self.assertGreater(self.controller._map_gaze((0.9, 0.0))[0], center)

    def test_up_and_down_gaze_map_in_consistent_vertical_directions(self):
        self.controller.calibration = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.5))
        center = self.controller._map_gaze((0.5, 0.0))[1]
        self.assertLess(self.controller._map_gaze((0.5, -0.4))[1], center)
        self.assertGreater(self.controller._map_gaze((0.5, 0.4))[1], center)

    def test_diagonal_gaze_maps_both_axes(self):
        self.controller.calibration = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.5))
        target = self.controller._map_gaze((0.1, -0.4))
        self.assertLess(target[0], 640)
        self.assertLess(target[1], 360)

    def test_processed_gaze_moves_cursor_in_each_direction(self):
        self.controller.calibration = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.5))
        self.controller.smoothing = 1.0
        self.controller.dead_zone = 0.0
        self.controller.max_cursor_step = 2000
        centered_x = lambda x: abs(x - 640) <= 1
        centered_y = lambda y: abs(y - 360) <= 1
        cases = [((0.1, 0.0), lambda x, y: x < 640 and centered_y(y)),
                 ((0.9, 0.0), lambda x, y: x > 640 and centered_y(y)),
                 ((0.5, -0.4), lambda x, y: centered_x(x) and y < 360),
                 ((0.5, 0.4), lambda x, y: centered_x(x) and y > 360),
                 ((0.1, -0.4), lambda x, y: x < 640 and y < 360)]
        for gaze, check in cases:
            with self.subTest(gaze=gaze):
                fake_pyautogui.moveTo.reset_mock()
                self.controller.previous = (640, 360)
                self.observe(gaze, at=self.clock.now + 0.05)
                self.assertTrue(fake_pyautogui.moveTo.called)
                x, y = fake_pyautogui.moveTo.call_args.args[:2]
                self.assertTrue(check(x, y), (x, y))

    def test_screen_mapping_clamps_to_primary_display_bounds(self):
        self.controller.calibration = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.5))
        self.assertEqual(self.controller._map_gaze((-0.5, 0.8)), (0, 719))
        self.assertEqual(self.controller._map_gaze((1.5, -0.8)), (1279, 0))

    def test_smoothing_limits_one_cursor_step(self):
        self.controller.previous = (100, 100)
        self.controller.smoothing = 0.5
        self.controller.max_cursor_step = 1000
        self.controller._move_cursor((300, 200))
        fake_pyautogui.moveTo.assert_called_once_with(200, 150, _pause=False)

    def test_sudden_gaze_change_is_capped_per_frame(self):
        self.controller.previous = (100, 100)
        self.controller.smoothing = 1.0
        self.controller.dead_zone = 0.0
        self.controller.max_cursor_step = 50.0
        self.controller._move_cursor((1000, 700))
        fake_pyautogui.moveTo.assert_called_once_with(150, 150, _pause=False)

    def test_dead_zone_ignores_small_coordinate_noise(self):
        self.controller.previous = (100, 100)
        self.controller._move_cursor((103, 102))
        fake_pyautogui.moveTo.assert_not_called()

    def test_invalid_or_missing_iris_disables_gaze_sample(self):
        self.assertIsNone(self.controller._estimate_gaze(self.landmarks(valid=False), (640, 480)))
        self.assertIsNone(self.controller._estimate_gaze([], (640, 480)))
        self.observe(landmarks=self.landmarks(valid=False))
        self.assertIsNone(self.controller._latest_gaze)
        fake_pyautogui.moveTo.assert_not_called()

    def test_incomplete_calibration_never_moves_cursor(self):
        self.observe()
        self.assertTrue(self.controller.calibrate(self.frame))
        self.observe((0.55, 0.0), at=0.1)
        self.assertIsNone(self.controller.calibration)
        fake_pyautogui.moveTo.assert_not_called()

    def _capture_target(self, target_name, gaze):
        self.observe(gaze, at=self.clock.now + 0.01)
        self.assertTrue(self.controller.calibrate(self.frame))
        remaining = self.controller.calibration_samples - 1
        for _ in range(remaining):
            self.observe(gaze, at=self.clock.now + 0.02)
        if target_name != "bottom":
            self.assertIn(target_name, self.controller._calibration_points)

    def test_calibration_averages_multiple_samples_per_target_and_fits_mapping(self):
        features = {
            "center": (0.5, 0.0), "left": (0.26, 0.0), "right": (0.74, 0.0),
            "top": (0.5, -0.24), "bottom": (0.5, 0.24),
        }
        self.observe(features["center"], at=0.01)
        self.assertTrue(self.controller.calibrate(self.frame))
        for index in range(self.controller.calibration_samples - 1):
            jitter = (0.002 if index % 2 else -0.002)
            self.observe((0.5 + jitter, 0.0), at=self.clock.now + 0.02)
        self.assertAlmostEqual(self.controller._calibration_points["center"][0], 0.5, places=3)
        for name in ("left", "right", "top", "bottom"):
            self._capture_target(name, features[name])
        self.assertIsNotNone(self.controller.calibration)
        self.assertIn("complete", self.status.call_args.args[0].lower())
        self.assertLess(self.controller._map_gaze(features["left"])[0], 640)
        self.assertGreater(self.controller._map_gaze(features["right"])[0], 640)
        self.assertLess(self.controller._map_gaze(features["top"])[1], 360)
        self.assertGreater(self.controller._map_gaze(features["bottom"])[1], 360)

    def test_unstable_calibration_samples_are_rejected(self):
        self.observe((0.5, 0.0), at=0.01)
        self.controller.calibrate(self.frame)
        for index in range(self.controller.calibration_samples - 1):
            self.controller._capture_calibration_sample((0.2 if index % 2 else 0.8, 0.0))
        self.assertNotIn("center", self.controller._calibration_points)
        self.assertIsNone(self.controller.calibration)
        self.assertIn("unstable", self.status.call_args.args[0].lower())

    def test_gaze_changes_do_not_trigger_blink_actions(self):
        self.controller.calibration = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.5))
        self.controller.previous = (640, 360)
        self.observe((0.2, -0.2), at=0.01)
        self.observe((0.8, 0.2), at=0.05)
        self.assertGreater(fake_pyautogui.moveTo.call_count, 0)
        fake_pyautogui.click.assert_not_called()
        fake_pyautogui.doubleClick.assert_not_called()


if __name__ == "__main__":
    unittest.main()
