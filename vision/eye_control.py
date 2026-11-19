import math
import logging
import statistics
import threading
import time

import mediapipe as mp
import numpy as np
import pyautogui
from desktop_input import DEFAULT_INPUT_COORDINATOR
from vision.frame_utils import (
    FrameNormalizationError,
    normalize_bgr_frame,
    validate_canonical_bgr_frame,
)


logger = logging.getLogger(__name__)


class InvalidCameraFrame(ValueError):
    """Raised when a camera frame cannot safely be passed to MediaPipe."""

    pipeline_stage = "frame_conversion"


class MediaPipeProcessingError(RuntimeError):
    """Preserves the original FaceMesh exception with an explicit stage."""

    pipeline_stage = "mediapipe_processing"


class LandmarkDrawingError(RuntimeError):
    """Preserves the OpenCV drawing failure and identifies its stage."""

    pipeline_stage = "landmark_drawing"


class EyeController:
    """Maps normalized iris movement to the screen and recognizes deliberate blinks."""

    def __init__(self, on_status=None, *, clock=None, action_dispatcher=None,
                 input_coordinator=None,
                 blink_threshold=0.16, blink_release_threshold=0.20,
                 minimum_blink_duration=0.06, maximum_blink_duration=0.45,
                 bilateral_window=0.18, minimum_bilateral_overlap=0.04,
                 minimum_bilateral_duration=0.18, blink_cooldown=0.10,
                 smoothing=0.35, dead_zone=4.0, max_cursor_step=120.0,
                 calibration_samples=15, calibration_min_samples=10,
                 calibration_outlier_tolerance=0.08, calibration_max_std=0.035):
        if not 0.0 < blink_threshold < blink_release_threshold < 1.0:
            raise ValueError("Blink EAR thresholds must satisfy 0 < closed < open < 1")
        if not 0.0 < minimum_blink_duration <= maximum_blink_duration:
            raise ValueError("Blink duration limits are invalid")
        if not 0.0 < bilateral_window <= maximum_blink_duration:
            raise ValueError("Bilateral blink window is invalid")
        if not 0.0 <= minimum_bilateral_overlap <= maximum_blink_duration:
            raise ValueError("Minimum bilateral overlap is invalid")
        if not minimum_blink_duration <= minimum_bilateral_duration <= maximum_blink_duration:
            raise ValueError("Minimum bilateral blink duration is invalid")
        if blink_cooldown < 0.0:
            raise ValueError("Blink cooldown cannot be negative")
        if not 0.0 < smoothing <= 1.0 or dead_zone < 0.0 or max_cursor_step <= 0.0:
            raise ValueError("Cursor smoothing settings are invalid")
        if calibration_samples < 5 or not 3 <= calibration_min_samples <= calibration_samples:
            raise ValueError("Calibration sample counts are invalid")
        if calibration_outlier_tolerance <= 0.0 or calibration_max_std <= 0.0:
            raise ValueError("Calibration stability settings are invalid")
        self.face_mesh = mp.solutions.face_mesh.FaceMesh(
            max_num_faces=1, refine_landmarks=True,
            min_detection_confidence=0.6, min_tracking_confidence=0.6,
        )
        self.screen_w, self.screen_h = self._read_screen_size()
        self.on_status = on_status or (lambda _message: None)
        self.input_coordinator = input_coordinator or DEFAULT_INPUT_COORDINATOR
        self._control_lock = threading.RLock()
        self._tracking_paused = False
        self._resume_arming = False
        self._reset_position_pending = False
        self.calibration = None
        self.previous = None
        self.smoothing = float(smoothing)
        self.dead_zone = float(dead_zone)
        self.max_cursor_step = float(max_cursor_step)
        self.calibration_samples = int(calibration_samples)
        self.calibration_min_samples = int(calibration_min_samples)
        self.calibration_outlier_tolerance = float(calibration_outlier_tolerance)
        self.calibration_max_std = float(calibration_max_std)
        self._calibration_targets = (
            ("center", (0.5, 0.5)), ("left", (0.1, 0.5)),
            ("right", (0.9, 0.5)), ("top", (0.5, 0.1)),
            ("bottom", (0.5, 0.9)),
        )
        self._calibration_points = {}
        self._calibration_index = None
        self._calibration_collecting = False
        self._calibration_buffer = []
        self._latest_gaze = None
        self._latest_gaze_time = None
        # EAR scales lid opening by eye width. The default 0.16 is an initial
        # normalized equivalent of the former 0.018 image-height gap for a
        # typical eye width; it is configurable and still needs camera tuning.
        self.blink_threshold = float(blink_threshold)
        self.blink_release_threshold = float(blink_release_threshold)
        self.minimum_blink_duration = float(minimum_blink_duration)
        self.maximum_blink_duration = float(maximum_blink_duration)
        self.bilateral_window = float(bilateral_window)
        self.minimum_bilateral_overlap = float(minimum_bilateral_overlap)
        self.minimum_bilateral_duration = float(minimum_bilateral_duration)
        self.blink_cooldown = float(blink_cooldown)
        self._clock = clock or time.monotonic
        self._action_dispatcher = (
            self._dispatch_blink_action if action_dispatcher is None else
            lambda action: self.input_coordinator.run_eye_action(
                "click", lambda: action_dispatcher(action)
            )
        )
        self.eye_closed_since = {"left": None, "right": None}
        self.eye_fired = {"left": False, "right": False}
        self.blink_events = []
        self._invalid_closure = {"left": False, "right": False}
        self._blink_suppression_state = None
        self._last_action = {"left": float("-inf"), "right": float("-inf"), "double": float("-inf")}
        self.closed = False

    @property
    def tracking_paused(self):
        with self._control_lock:
            return self._tracking_paused

    def pause_tracking(self):
        if not self.input_coordinator.pause_eye():
            return False
        with self._control_lock:
            self._tracking_paused = True
            self._resume_arming = False
            self._reset_position_pending = True
        return True

    def resume_tracking(self):
        if not self.input_coordinator.resume_eye():
            return False
        with self._control_lock:
            self._tracking_paused = False
            self._resume_arming = True
            self._reset_position_pending = True
        return True

    @staticmethod
    def _read_screen_size():
        try:
            width, height = pyautogui.size()
            width, height = int(width), int(height)
            return (width, height) if width > 1 and height > 1 else (0, 0)
        except Exception:
            return 0, 0

    def calibrate(self, frame):
        """Start/advance a target capture; process_frame collects live samples.

        The existing one-frame call surface is retained. Each successful call
        captures the current target from that live frame, then process_frame
        gathers multiple subsequent observations while the user holds gaze.
        """
        del frame  # A recent, validated gaze sample is cached by process_frame.
        now = self._clock()
        if self._latest_gaze is None or self._latest_gaze_time is None or now - self._latest_gaze_time > 0.5:
            self.on_status("Calibration needs a recent, visible, open-eye gaze sample")
            return False

        if self._calibration_index is None or self.calibration is not None:
            self.calibration = None
            self.previous = None
            self._calibration_points = {}
            self._calibration_index = 0
        elif self._calibration_collecting:
            name = self._calibration_targets[self._calibration_index][0]
            self.on_status("Keep looking at the {} target; collecting samples.".format(name))
            return False
        elif self._calibration_targets[self._calibration_index][0] in self._calibration_points:
            if self._calibration_index == len(self._calibration_targets) - 1:
                return self.calibration is not None
            self._calibration_index += 1

        self._calibration_buffer = [self._latest_gaze]
        self._calibration_collecting = True
        name = self._calibration_targets[self._calibration_index][0]
        self.on_status("Look at the screen {} target and hold steady: sample 1/{}".format(
            name, self.calibration_samples))
        return True

    def process_frame(self, frame):
        try:
            try:
                bgr_frame = validate_canonical_bgr_frame(frame)
            except FrameNormalizationError:
                bgr_frame = normalize_bgr_frame(frame)
        except FrameNormalizationError as exc:
            raise InvalidCameraFrame(str(exc)) from exc
        rgb_frame = self._prepare_rgb_frame(bgr_frame, canonical=True)
        try:
            result = self.face_mesh.process(rgb_frame)
        except Exception as exc:
            raise MediaPipeProcessingError(
                "FaceMesh.process failed with {}: {}".format(type(exc).__name__, exc)
            ) from exc
        if not result.multi_face_landmarks:
            self._reset_blink_tracking()
            self._latest_gaze = None
            self._latest_gaze_time = None
            return bgr_frame
        landmarks = result.multi_face_landmarks[0].landmark
        now = self._clock()
        height, width = bgr_frame.shape[:2]
        with self._control_lock:
            paused = self._tracking_paused
            arming = self._resume_arming
        if paused or not self.input_coordinator.eye_actions_enabled:
            reason = "eye_paused" if paused or self.input_coordinator.eye_paused else "emergency_stop"
            if self._blink_suppression_state != reason:
                logger.warning("BLINK suppression reason=%s", reason)
                self._blink_suppression_state = reason
            self._reset_blink_tracking()
        elif arming:
            if self._blink_suppression_state != "resume_arming":
                logger.info("BLINK suppression reason=resume_arming")
                self._blink_suppression_state = "resume_arming"
            self._reset_blink_tracking()
            try:
                both_open = all(
                    self._eye_aspect_ratio(landmarks, side, (width, height)) >= self.blink_release_threshold
                    for side in ("left", "right")
                )
            except (AttributeError, IndexError, TypeError, ValueError, OverflowError):
                both_open = False
            if both_open:
                with self._control_lock:
                    self._resume_arming = False
        else:
            self._blink_suppression_state = None
            self._process_blinks(landmarks, (width, height), now)
        gaze = self._estimate_gaze(landmarks, (width, height))
        self._latest_gaze = gaze
        self._latest_gaze_time = now if gaze is not None else None
        if gaze is not None and self._calibration_collecting:
            self._capture_calibration_sample(gaze)
        if gaze is not None and self.calibration is not None:
            target = self._map_gaze(gaze)
            if target is not None:
                self._move_cursor(target)
        return self._draw_face_landmarks(bgr_frame, landmarks, canonical=True)

    @staticmethod
    def _draw_face_landmarks(frame, landmarks, canonical=False):
        """Draw subtle landmark dots on a separate BGR preview copy."""
        try:
            bgr_frame = validate_canonical_bgr_frame(frame) if canonical else normalize_bgr_frame(frame)
        except FrameNormalizationError as exc:
            raise InvalidCameraFrame(str(exc)) from exc
        height, width = bgr_frame.shape[:2]
        points = []
        for landmark in landmarks:
            try:
                x = float(landmark.x)
                y = float(landmark.y)
                if not math.isfinite(x) or not math.isfinite(y):
                    continue
                if not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0:
                    continue
                points.append((int(round(x * (width - 1))), int(round(y * (height - 1)))))
            except (AttributeError, TypeError, ValueError, OverflowError):
                continue
        if not points:
            return bgr_frame

        # Drawing gets a writable active-NumPy BGR array, separate from FaceMesh input.
        annotated = normalize_bgr_frame(bgr_frame)
        try:
            yy, xx = np.ogrid[-2:3, -2:3]
            alpha = np.clip(2.5 - np.sqrt(xx * xx + yy * yy), 0.0, 1.0)[..., None]
            color = np.asarray((40, 235, 170), dtype=np.float32)
            for point in points:
                center_x, center_y = point
                x0, x1 = max(0, center_x - 2), min(width, center_x + 3)
                y0, y1 = max(0, center_y - 2), min(height, center_y + 3)
                if x0 >= x1 or y0 >= y1:
                    continue
                mask_x0, mask_y0 = x0 - (center_x - 2), y0 - (center_y - 2)
                mask = alpha[mask_y0:mask_y0 + (y1 - y0), mask_x0:mask_x0 + (x1 - x0)]
                region = annotated[y0:y1, x0:x1].astype(np.float32)
                annotated[y0:y1, x0:x1] = np.rint(region * (1.0 - mask) + color * mask).astype(np.uint8)
        except Exception as exc:
            raise LandmarkDrawingError("Landmark drawing failed with {}: {}".format(
                type(exc).__name__, exc
            )) from exc
        return annotated

    @staticmethod
    def _prepare_rgb_frame(frame, canonical=False):
        """Normalize a valid BGR camera frame to MediaPipe's RGB ndarray input.

        OpenCV and MediaPipe wheels can be compiled against different NumPy
        ABIs. Copying through the active NumPy runtime gives the MediaPipe
        binding an ordinary, owned ndarray rather than an OpenCV-backed view.
        Invalid shape or dtype is rejected instead of silently coerced.
        """
        try:
            bgr_frame = validate_canonical_bgr_frame(frame) if canonical else normalize_bgr_frame(frame)
            # Separate owned RGB buffer for the MediaPipe pybind boundary.
            rgb_frame = np.array(
                bgr_frame[:, :, ::-1], dtype=np.uint8, copy=True, order="C"
            )
        except FrameNormalizationError as exc:
            raise InvalidCameraFrame("Camera frame could not be normalized") from exc
        if (rgb_frame.dtype != np.uint8 or rgb_frame.ndim != 3
                or rgb_frame.shape[2] != 3 or not rgb_frame.flags.c_contiguous
                or not rgb_frame.flags.writeable or not rgb_frame.flags.owndata):
            raise InvalidCameraFrame("Normalized RGB camera frame is invalid")
        return rgb_frame

    @staticmethod
    def _eye_gaze_feature(landmarks, corner_indices, iris_index, frame_size):
        width, height = frame_size
        first, second = (EyeController._point(landmarks, index) for index in corner_indices)
        iris = EyeController._point(landmarks, iris_index)
        a = (first[0] * width, first[1] * height)
        b = (second[0] * width, second[1] * height)
        pupil = (iris[0] * width, iris[1] * height)
        if a[0] > b[0]:
            a, b = b, a
        dx, dy = b[0] - a[0], b[1] - a[1]
        length_sq = dx * dx + dy * dy
        length = math.sqrt(length_sq)
        if length < 8.0:
            raise ValueError("eye corners are too close to estimate gaze")
        offset_x, offset_y = pupil[0] - a[0], pupil[1] - a[1]
        horizontal = (offset_x * dx + offset_y * dy) / length_sq
        midpoint_x, midpoint_y = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
        # With corners ordered left-to-right, this perpendicular points mostly down.
        vertical = ((pupil[0] - midpoint_x) * -dy
                    + (pupil[1] - midpoint_y) * dx) / length_sq
        if not (-0.2 <= horizontal <= 1.2 and abs(vertical) <= 0.45):
            raise ValueError("iris is outside the reliable eye region")
        return horizontal, vertical

    def _estimate_gaze(self, landmarks, frame_size):
        """Average both open eyes in eye-local coordinates to reduce head translation/scale."""
        try:
            if any(self._eye_aspect_ratio(landmarks, side, frame_size) < self.blink_release_threshold
                   for side in ("left", "right")):
                return None
            left = self._eye_gaze_feature(landmarks, (33, 133), 468, frame_size)
            right = self._eye_gaze_feature(landmarks, (362, 263), 473, frame_size)
        except (AttributeError, IndexError, TypeError, ValueError, OverflowError):
            return None
        # A large inter-eye disagreement is more likely a bad landmark frame than gaze.
        if abs(left[0] - right[0]) > 0.30 or abs(left[1] - right[1]) > 0.20:
            return None
        return ((left[0] + right[0]) / 2.0, (left[1] + right[1]) / 2.0)

    def _capture_calibration_sample(self, gaze):
        if not self._calibration_collecting:
            return
        self._calibration_buffer.append(gaze)
        count = len(self._calibration_buffer)
        if count < self.calibration_samples:
            if count % 5 == 0:
                name = self._calibration_targets[self._calibration_index][0]
                self.on_status("{} calibration: sample {}/{}".format(name.title(), count,
                                                                       self.calibration_samples))
            return

        average = self._stable_average(self._calibration_buffer)
        name = self._calibration_targets[self._calibration_index][0]
        self._calibration_collecting = False
        self._calibration_buffer = []
        if average is None:
            self.on_status("{} calibration was unstable; hold gaze steady and activate calibration to retry.".format(name.title()))
            return
        self._calibration_points[name] = average
        if self._calibration_index == len(self._calibration_targets) - 1:
            if not self._finish_calibration():
                self._calibration_points = {}
                self._calibration_index = None
                self.calibration = None
                if self.screen_w < 2 or self.screen_h < 2:
                    self.on_status("Primary display resolution is unavailable; gaze control remains disabled.")
                else:
                    self.on_status("Calibration points were inconsistent; please restart calibration.")
            return
        next_name = self._calibration_targets[self._calibration_index + 1][0]
        self.on_status("{} captured. Look at the {} screen target, then activate Calibrate gaze.".format(
            name.title(), next_name))

    def _stable_average(self, samples):
        median_x = statistics.median(sample[0] for sample in samples)
        median_y = statistics.median(sample[1] for sample in samples)
        stable = [sample for sample in samples
                  if math.hypot(sample[0] - median_x, sample[1] - median_y)
                  <= self.calibration_outlier_tolerance]
        if len(stable) < self.calibration_min_samples:
            return None
        mean_x = sum(sample[0] for sample in stable) / len(stable)
        mean_y = sum(sample[1] for sample in stable) / len(stable)
        std_x = math.sqrt(sum((sample[0] - mean_x) ** 2 for sample in stable) / len(stable))
        std_y = math.sqrt(sum((sample[1] - mean_y) ** 2 for sample in stable) / len(stable))
        if max(std_x, std_y) > self.calibration_max_std:
            return None
        return mean_x, mean_y

    @staticmethod
    def _solve_3x3(matrix, vector):
        rows = [list(map(float, row)) + [float(value)] for row, value in zip(matrix, vector)]
        for column in range(3):
            pivot = max(range(column, 3), key=lambda row: abs(rows[row][column]))
            if abs(rows[pivot][column]) < 1e-10:
                raise ValueError("Calibration gaze points are degenerate")
            rows[column], rows[pivot] = rows[pivot], rows[column]
            divisor = rows[column][column]
            rows[column] = [value / divisor for value in rows[column]]
            for row in range(3):
                if row == column:
                    continue
                factor = rows[row][column]
                rows[row] = [value - factor * pivot_value
                             for value, pivot_value in zip(rows[row], rows[column])]
        return tuple(rows[row][3] for row in range(3))

    def _finish_calibration(self):
        if len(self._calibration_points) != len(self._calibration_targets):
            return False
        self.screen_w, self.screen_h = self._read_screen_size()
        if self.screen_w < 2 or self.screen_h < 2:
            return False
        rows, screen_x, screen_y = [], [], []
        for name, target in self._calibration_targets:
            gaze = self._calibration_points[name]
            rows.append((gaze[0], gaze[1], 1.0))
            screen_x.append(target[0])
            screen_y.append(target[1])
        normal = [[sum(row[i] * row[j] for row in rows) for j in range(3)] for i in range(3)]
        rhs_x = [sum(row[i] * value for row, value in zip(rows, screen_x)) for i in range(3)]
        rhs_y = [sum(row[i] * value for row, value in zip(rows, screen_y)) for i in range(3)]
        try:
            x_map = self._solve_3x3(normal, rhs_x)
            y_map = self._solve_3x3(normal, rhs_y)
        except ValueError:
            return False
        errors = []
        for row, target_x, target_y in zip(rows, screen_x, screen_y):
            predicted_x = sum(coef * value for coef, value in zip(x_map, row))
            predicted_y = sum(coef * value for coef, value in zip(y_map, row))
            errors.append(math.hypot(predicted_x - target_x, predicted_y - target_y))
        if max(errors) > 0.15:
            return False
        self.calibration = (x_map, y_map)
        self.previous = None
        self.on_status("Five-point gaze calibration complete; cursor control enabled.")
        return True

    def _map_gaze(self, gaze):
        if self.calibration is None:
            return None
        self.screen_w, self.screen_h = self._read_screen_size()
        if self.screen_w < 2 or self.screen_h < 2:
            return None
        x_map, y_map = self.calibration
        feature = (gaze[0], gaze[1], 1.0)
        normalized_x = sum(coef * value for coef, value in zip(x_map, feature))
        normalized_y = sum(coef * value for coef, value in zip(y_map, feature))
        if not (math.isfinite(normalized_x) and math.isfinite(normalized_y)):
            return None
        x = round(max(0.0, min(1.0, normalized_x)) * (self.screen_w - 1))
        y = round(max(0.0, min(1.0, normalized_y)) * (self.screen_h - 1))
        return int(x), int(y)

    def _move_cursor(self, target):
        with self._control_lock:
            if self._reset_position_pending:
                self.previous = None
                self._reset_position_pending = False
        if self.previous is None:
            try:
                current = pyautogui.position()
                if (not isinstance(current, (tuple, list)) or len(current) != 2
                        or any(not math.isfinite(float(value)) for value in current)):
                    current = target
                current_x = max(0, min(self.screen_w - 1, int(current[0])))
                current_y = max(0, min(self.screen_h - 1, int(current[1])))
            except (OSError, RuntimeError, TypeError, ValueError):
                current_x, current_y = target
            self.previous = (current_x, current_y)

        dx, dy = target[0] - self.previous[0], target[1] - self.previous[1]
        if max(abs(dx), abs(dy)) <= self.dead_zone:
            return
        dx = max(-self.max_cursor_step, min(self.max_cursor_step, dx))
        dy = max(-self.max_cursor_step, min(self.max_cursor_step, dy))
        x = round(self.previous[0] + self.smoothing * dx)
        y = round(self.previous[1] + self.smoothing * dy)
        x = max(0, min(self.screen_w - 1, x))
        y = max(0, min(self.screen_h - 1, y))
        if (x, y) != self.previous:
            moved = self.input_coordinator.run_eye_action(
                "move", lambda: pyautogui.moveTo(x, y, _pause=False), target=(x, y)
            )
            if moved:
                self.previous = (x, y)

    @staticmethod
    def _point(landmarks, index):
        point = landmarks[index]
        x, y = float(point.x), float(point.y)
        if not (math.isfinite(x) and math.isfinite(y)
                and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("invalid eye landmark")
        return x, y

    @classmethod
    def _eye_aspect_ratio(cls, landmarks, side, frame_size):
        # These are MediaPipe's anatomical eye groups. Horizontal preview
        # mirroring changes pixel position, not the landmark semantic labels.
        if side == "left":
            corners, vertical_pairs = (362, 263), ((386, 374), (387, 373))
        else:
            corners, vertical_pairs = (33, 133), ((159, 145), (158, 153))
        width, height = frame_size
        p1, p2 = (cls._point(landmarks, i) for i in corners)
        eye_width = math.hypot((p1[0] - p2[0]) * width, (p1[1] - p2[1]) * height)
        if eye_width < 8.0:
            raise ValueError("eye landmarks too close to measure reliably")
        vertical_distances = []
        for upper, lower in vertical_pairs:
            a, b = cls._point(landmarks, upper), cls._point(landmarks, lower)
            vertical_distances.append(math.hypot((a[0] - b[0]) * width,
                                                (a[1] - b[1]) * height))
        ear = sum(vertical_distances) / (2.0 * eye_width)
        if not math.isfinite(ear) or not 0.0 <= ear <= 1.0:
            raise ValueError("unstable eye aspect ratio")
        return ear

    def _reset_blink_tracking(self):
        self.eye_closed_since = {"left": None, "right": None}
        self.eye_fired = {"left": False, "right": False}
        self._invalid_closure = {"left": False, "right": False}
        self.blink_events.clear()

    def _process_blinks(self, landmarks, frame_size, now):
        """Update independent eye states, pair overlapping blinks, then dispatch singles."""
        try:
            ears = {side: self._eye_aspect_ratio(landmarks, side, frame_size)
                    for side in ("left", "right")}
        except (AttributeError, IndexError, TypeError, ValueError, OverflowError):
            # Never turn a partial/noisy face observation into a click. Discard
            # an in-progress candidate so invalid frames cannot bridge a blink.
            self._reset_blink_tracking()
            return

        completed = []
        for side, ear in ears.items():
            started = self.eye_closed_since[side]
            if started is None:
                if ear <= self.blink_threshold:
                    self.eye_closed_since[side] = now
                    self.eye_fired[side] = False
                    self._invalid_closure[side] = False
                    logger.info("BLINK event=eye_closed side=%s ear=%.4f", side, ear)
            elif ear >= self.blink_release_threshold:
                duration = now - started
                self.eye_closed_since[side] = None
                logger.info("BLINK event=eye_reopened side=%s ear=%.4f duration=%.3f",
                            side, ear, duration)
                if (not self._invalid_closure[side]
                        and self.minimum_blink_duration <= duration <= self.maximum_blink_duration):
                    completed.append({"side": side, "start": started, "end": now})
                    self.eye_fired[side] = True
                else:
                    self.eye_fired[side] = False
                self._invalid_closure[side] = False
            elif now - started > self.maximum_blink_duration:
                # Keep the closed state latched until reopening, but suppress it.
                self._invalid_closure[side] = True
                self.eye_fired[side] = True

        self.blink_events.extend(completed)
        paired = set()
        for left_index, left in enumerate(self.blink_events):
            if left["side"] != "left" or left_index in paired:
                continue
            for right_index, right in enumerate(self.blink_events):
                if right_index == left_index:
                    continue
                if right["side"] != "right" or right_index in paired:
                    continue
                overlap = min(left["end"], right["end"]) - max(left["start"], right["start"])
                starts_close = abs(left["start"] - right["start"]) <= self.bilateral_window
                if overlap >= self.minimum_bilateral_overlap and starts_close:
                    paired.update((left_index, right_index))
                    # Short synchronized closures are treated as ordinary
                    # natural blinks and suppressed; intentional bilateral
                    # activation requires a deliberate, longer closure.
                    deliberate = min(left["end"] - left["start"],
                                     right["end"] - right["start"]) >= self.minimum_bilateral_duration
                    if deliberate:
                        self._fire_blink_action("double", now)
                    break

        remaining = []
        for index, event in enumerate(self.blink_events):
            if index in paired:
                continue
            other = "right" if event["side"] == "left" else "left"
            other_started = self.eye_closed_since[other]
            could_overlap = (other_started is not None and not self._invalid_closure[other]
                             and other_started <= event["end"]
                             and event["start"] - self.bilateral_window <= other_started)
            if could_overlap:
                remaining.append(event)
            else:
                self._fire_blink_action(event["side"], now)
        self.blink_events = remaining

    def _fire_blink_action(self, action, now):
        logger.info("BLINK event=click_generated action=%s", action)
        sides = ("left", "right") if action == "double" else (action,)
        if any(now - self._last_action[side] < self.blink_cooldown for side in sides):
            logger.info("BLINK click_rejected action=%s reason=cooldown", action)
            return False
        try:
            dispatched = self._action_dispatcher(action)
        except Exception:
            logger.exception("BLINK click_transaction_failed action=%s", action)
            raise
        if dispatched is False:
            logger.info("BLINK click_rejected action=%s reason=coordinator", action)
            return False
        logger.info("BLINK click_transaction_accepted action=%s", action)
        for side in sides:
            self._last_action[side] = now
        if action == "double":
            self._last_action["double"] = now
            self.on_status("Both-eye blink · double click")
        elif action == "left":
            self.on_status("Left-eye blink · left click")
        else:
            self.on_status("Right-eye blink · right click")
        return True

    def _dispatch_blink_action(self, action):
        """Desktop action boundary, replaceable with a mock in hardware-free tests."""
        if action not in {"left", "right", "double"}:
            raise ValueError("Unknown blink action: {}".format(action))
        def click():
            button = "right" if action == "right" else "left"
            click_count = 2 if action == "double" else 1
            for click_index in range(click_count):
                if click_index and self.input_coordinator.emergency_active:
                    logger.warning("INPUT click_cancelled action=%s reason=emergency_stop", action)
                    break
                try:
                    try:
                        position = tuple(pyautogui.position())
                    except Exception:
                        position = "unknown"
                    logger.info("INPUT mouse_down action=%s button=%s position=%s thread=%s",
                                action, button, position, threading.get_ident())
                    pyautogui.mouseDown(button=button, _pause=False)
                finally:
                    # Release even when the press call raises or emergency stop
                    # is latched between the press and release.
                    try:
                        pyautogui.mouseUp(button=button, _pause=False)
                        logger.info("INPUT mouse_up action=%s button=%s position=%s thread=%s",
                                    action, button, position, threading.get_ident())
                    except Exception:
                        logger.exception("INPUT mouse_up_failed action=%s button=%s", action, button)
                        raise
        accepted = self.input_coordinator.run_eye_action("click", click)
        logger.info("INPUT click_transaction_%s action=%s",
                    "accepted" if accepted else "rejected", action)
        return accepted

    def close(self):
        if not self.closed:
            self.face_mesh.close()
            self.closed = True
