import threading
import time

import cv2


class CameraPipeline:
    def __init__(self, eye_controller=None, on_frame=None, on_status=None, camera_index=0):
        self.eye_controller = eye_controller
        self.on_frame = on_frame or (lambda _frame: None)
        self.on_status = on_status or (lambda _message: None)
        self.camera_index = camera_index
        self.running = False
        self.cap = None
        self.thread = None
        self.latest_frame = None
        self.frame_lock = threading.Lock()

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self.loop, name="SynapseCamera", daemon=True)
        self.thread.start()

    def loop(self):
        try:
            self.cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
            if not self.cap.isOpened():
                self.on_status("Camera unavailable")
                return
            self.on_status("Camera connected")
            while self.running:
                ok, frame = self.cap.read()
                if not ok:
                    self.on_status("Camera frame unavailable")
                    time.sleep(0.1)
                    continue
                frame = cv2.flip(frame, 1)
                with self.frame_lock:
                    self.latest_frame = frame.copy()
                if self.eye_controller:
                    frame = self.eye_controller.process_frame(frame)
                self.on_frame(frame)
        except Exception as exc:
            self.on_status("Camera error: {}".format(exc))
        finally:
            if self.cap:
                self.cap.release()
                self.cap = None
            self.running = False
            self.on_status("Camera stopped")

    def calibrate(self):
        with self.frame_lock:
            frame = None if self.latest_frame is None else self.latest_frame.copy()
        if frame is None or not self.eye_controller:
            self.on_status("Start eye tracking before calibration")
            return False
        return self.eye_controller.calibrate(frame)

    def reset_controller(self):
        if self.eye_controller:
            self.eye_controller.close()
            self.eye_controller = None

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=3)
        if self.eye_controller:
            self.eye_controller.close()
