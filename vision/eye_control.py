import time

import cv2
import mediapipe as mp
import pyautogui


class EyeController:
    """Maps normalized iris movement to the screen and recognizes deliberate blinks."""

    def __init__(self, on_status=None):
        self.face_mesh = mp.solutions.face_mesh.FaceMesh(
            max_num_faces=1, refine_landmarks=True,
            min_detection_confidence=0.6, min_tracking_confidence=0.6,
        )
        self.screen_w, self.screen_h = pyautogui.size()
        self.on_status = on_status or (lambda _message: None)
        self.calibration = None
        self.previous = None
        self.smoothing = 0.28
        self.blink_threshold = 0.018
        self.eye_closed_since = {"left": None, "right": None}
        self.eye_fired = {"left": False, "right": False}
        self.blink_events = []
        self.closed = False

    def calibrate(self, frame):
        result = self.face_mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if not result.multi_face_landmarks:
            self.on_status("Calibration needs a visible face")
            return False
        landmarks = result.multi_face_landmarks[0].landmark
        iris = landmarks[474]
        self.calibration = (iris.x, iris.y, max(0.08, iris.x * 0.8), max(0.08, iris.y * 0.8))
        self.previous = None
        self.on_status("Gaze calibrated; move your eyes to control the pointer")
        return True

    def process_frame(self, frame):
        result = self.face_mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if not result.multi_face_landmarks:
            self.eye_closed_since = {"left": None, "right": None}
            return frame
        landmarks = result.multi_face_landmarks[0].landmark
        iris = landmarks[474]
        if self.calibration is None:
            self.calibration = (0.5, 0.5, 0.4, 0.4)
        center_x, center_y, range_x, range_y = self.calibration
        target_x = int(max(0, min(self.screen_w - 1, (iris.x - center_x) / range_x * self.screen_w + self.screen_w / 2)))
        target_y = int(max(0, min(self.screen_h - 1, (iris.y - center_y) / range_y * self.screen_h + self.screen_h / 2)))
        if self.previous is None:
            self.previous = (target_x, target_y)
        else:
            x = int(self.previous[0] + self.smoothing * (target_x - self.previous[0]))
            y = int(self.previous[1] + self.smoothing * (target_y - self.previous[1]))
            pyautogui.moveTo(x, y, _pause=False)
            self.previous = (x, y)
        left_closed = abs(landmarks[145].y - landmarks[159].y) < self.blink_threshold
        right_closed = abs(landmarks[374].y - landmarks[386].y) < self.blink_threshold
        now = time.monotonic()
        for side, closed in (("left", left_closed), ("right", right_closed)):
            if closed and self.eye_closed_since[side] is None:
                self.eye_closed_since[side] = now
            elif not closed and self.eye_closed_since[side] is not None:
                duration = now - self.eye_closed_since[side]
                self.eye_closed_since[side] = None
                if 0.04 <= duration <= 0.55 and not self.eye_fired[side]:
                    self.blink_events.append((now, side))
                    self.eye_fired[side] = True
            elif closed and self.eye_closed_since[side] is not None and now - self.eye_closed_since[side] > 0.45:
                self.eye_fired[side] = True
        self.blink_events = [(stamp, side) for stamp, side in self.blink_events if now - stamp <= 0.38]
        if len(self.blink_events) >= 2:
            first, second = self.blink_events[-2:]
            if first[1] == second[1] and second[0] - first[0] <= 0.38:
                pyautogui.doubleClick(_pause=False)
                self.blink_events.clear()
                self.on_status("Double blink · double click")
        elif self.blink_events and now - self.blink_events[0][0] > 0.38:
            _, side = self.blink_events.pop(0)
            if side == "left":
                pyautogui.click(_pause=False)
                self.on_status("Left-eye blink · left click")
            else:
                pyautogui.click(button="right", _pause=False)
                self.on_status("Right-eye blink · right click")
        if not left_closed:
            self.eye_fired["left"] = False
        if not right_closed:
            self.eye_fired["right"] = False
        return frame

    def close(self):
        if not self.closed:
            self.face_mesh.close()
            self.closed = True
