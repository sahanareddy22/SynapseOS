"""Single-worker camera capture and serialized eye-controller access."""
import logging
import threading
import time

import cv2
import numpy as np
from vision.frame_utils import (
    FrameNormalizationError,
    flip_bgr_horizontally,
    normalize_bgr_frame,
    validate_canonical_bgr_frame,
)


logger = logging.getLogger(__name__)
_DEVICE_LOCKS_GUARD = threading.Lock()
_DEVICE_LOCKS = {}


class InvalidCapturedFrame(ValueError):
    """Raised when VideoCapture does not yield a supported BGR image."""

    pipeline_stage = "capture_validation"


def _validate_capture_frame(frame):
    """Convert VideoCapture output into the pipeline's canonical BGR buffer."""
    try:
        return normalize_bgr_frame(frame)
    except FrameNormalizationError as exc:
        raise InvalidCapturedFrame(str(exc)) from exc


def _device_lock(camera_index):
    with _DEVICE_LOCKS_GUARD:
        return _DEVICE_LOCKS.setdefault(camera_index, threading.Lock())


class CameraPipeline:
    def __init__(self, eye_controller=None, on_frame=None, on_status=None, camera_index=0,
                 *, controller_factory=None, read_failure_limit=5, reconnect_attempts=3,
                 max_reconnect_cycles=3, retry_interval=0.5, stop_timeout=3.0,
                 processing_failure_limit=5):
        if (read_failure_limit < 1 or reconnect_attempts < 1 or max_reconnect_cycles < 0
                or processing_failure_limit < 1):
            raise ValueError("Camera retry limits must be positive")
        if retry_interval < 0 or stop_timeout < 0:
            raise ValueError("Camera timing values cannot be negative")
        self.eye_controller = eye_controller
        self.on_frame = on_frame or (lambda _frame: None)
        self.on_status = on_status or (lambda _message: None)
        self.camera_index = camera_index
        self.read_failure_limit = int(read_failure_limit)
        self.reconnect_attempts = int(reconnect_attempts)
        self.max_reconnect_cycles = int(max_reconnect_cycles)
        self.retry_interval = float(retry_interval)
        self.stop_timeout = float(stop_timeout)
        self.processing_failure_limit = int(processing_failure_limit)
        self.controller_factory = controller_factory or self._default_controller_factory(eye_controller)
        self.running = False
        self.cap = None
        self.thread = None
        self.latest_frame = None
        self.frame_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._calibration_requested = threading.Event()
        self._frame_metadata_logged = False
        self._preview_error_active = False

    @staticmethod
    def _default_controller_factory(controller):
        if controller is None:
            return None
        controller_type = type(controller)
        status_callback = getattr(controller, "on_status", None)
        if status_callback is None:
            return lambda: controller_type()
        return lambda: controller_type(on_status=status_callback)

    def _status(self, message):
        try:
            self.on_status(message)
        except Exception:
            logger.exception("Camera status callback failed")

    def _deliver_preview(self, frame):
        try:
            self.on_frame(frame)
        except Exception as exc:
            if not self._preview_error_active:
                logger.exception("Preview delivery failed at stage=preview_delivery")
                self._status("Camera preview error: {}".format(type(exc).__name__))
            self._preview_error_active = True
            return False
        if self._preview_error_active:
            self._status("Camera preview recovered")
            self._preview_error_active = False
        return True

    def start(self):
        with self._state_lock:
            if self.thread is not None and self.thread.is_alive():
                self._status("Camera worker is already running or still stopping")
                return False
            self._stop_event = threading.Event()
            self._calibration_requested.clear()
            with self.frame_lock:
                self.latest_frame = None
            self.running = True
            worker = threading.Thread(target=self.loop, name="SynapseCamera", daemon=True)
            self.thread = worker
            try:
                worker.start()
            except Exception:
                self.running = False
                self.thread = None
                self._close_controller_without_worker()
                self._status("Camera worker could not start")
                return False
        return True

    def _wait_retry(self, attempt):
        delay = self.retry_interval * min(2 ** max(0, attempt - 1), 4)
        return self._stop_event.wait(delay)

    def _open_camera(self, reconnecting=False):
        for attempt in range(1, self.reconnect_attempts + 1):
            if self._stop_event.is_set():
                return None
            cap = None
            try:
                cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
                if cap.isOpened():
                    with self._state_lock:
                        self.cap = cap
                    return cap
            except Exception as exc:
                logger.warning("Camera open attempt %d failed: %s", attempt, type(exc).__name__)
            self._release_capture(cap)
            if reconnecting:
                self._status("Camera reconnect attempt {}/{} failed".format(attempt, self.reconnect_attempts))
            else:
                self._status("Camera unavailable; open attempt {}/{} failed".format(attempt, self.reconnect_attempts))
            if attempt < self.reconnect_attempts and self._wait_retry(attempt):
                return None
        return None

    @staticmethod
    def _release_capture(cap):
        if cap is not None:
            try:
                cap.release()
            except Exception:
                logger.exception("Camera release failed")

    def _ensure_controller(self):
        with self._state_lock:
            controller = self.eye_controller
            factory = self.controller_factory
        if controller is None and factory is not None:
            controller = factory()
            with self._state_lock:
                self.eye_controller = controller
        return controller

    def loop(self):
        device_lock = _device_lock(self.camera_index)
        owns_device = False
        cap = None
        controller = None
        try:
            while not self._stop_event.is_set():
                owns_device = device_lock.acquire(timeout=0.1)
                if owns_device:
                    break
            if not owns_device or self._stop_event.is_set():
                return

            controller = self._ensure_controller()
            cap = self._open_camera(reconnecting=False)
            if cap is None:
                self._status("Camera unavailable; startup attempts exhausted")
                return

            connected = False
            failed_reads = 0
            failed_processing = 0
            reconnect_cycles = 0
            while self.running and not self._stop_event.is_set():
                try:
                    ok, frame = cap.read()
                except Exception as exc:
                    logger.exception("Camera read failed at stage=camera_read")
                    self._status("Camera read error: {}; checking connection".format(type(exc).__name__))
                    ok, frame = False, None

                if not ok or frame is None:
                    failed_reads += 1
                    if failed_reads == 1:
                        self._status("Camera frame unavailable; checking connection")
                    if failed_reads < self.read_failure_limit:
                        if self._stop_event.wait(min(self.retry_interval, 0.1)):
                            break
                        continue

                    self._status("Camera disconnected; starting bounded recovery")
                    self._release_capture(cap)
                    cap = None
                    with self._state_lock:
                        self.cap = None
                    reconnect_cycles += 1
                    if reconnect_cycles > self.max_reconnect_cycles:
                        self._status("Camera recovery attempts exhausted")
                        break
                    cap = self._open_camera(reconnecting=True)
                    if cap is None:
                        if not self._stop_event.is_set():
                            self._status("Camera recovery failed; tracking stopped")
                        break
                    failed_reads = 0
                    connected = False
                    continue

                failed_reads = 0
                if not self._frame_metadata_logged:
                    self._log_frame_metadata(frame)
                    self._frame_metadata_logged = True
                stage = "capture_validation"
                preview_frame = None
                try:
                    frame = _validate_capture_frame(frame)
                    stage = "horizontal_flip"
                    frame = flip_bgr_horizontally(frame)
                    preview_frame = frame
                    frame_copy = frame.copy()
                    with self.frame_lock:
                        self.latest_frame = frame_copy
                    if controller is not None:
                        stage = "eye_processing"
                        frame = controller.process_frame(frame)
                        stage = "eye_output_validation"
                        frame = validate_canonical_bgr_frame(frame)
                except (InvalidCapturedFrame, TypeError, ValueError, RuntimeError, cv2.error) as exc:
                    failed_processing += 1
                    failure_stage = getattr(exc, "pipeline_stage", stage)
                    if failed_processing == 1:
                        logger.exception("Camera frame failed at stage=%s; retrying", failure_stage)
                    else:
                        logger.warning("Camera frame failure stage=%s (%d/%d): %s",
                                       failure_stage, failed_processing, self.processing_failure_limit,
                                       type(exc).__name__)
                    if failed_processing >= self.processing_failure_limit:
                        detail = str(exc) if isinstance(exc, (InvalidCapturedFrame, ValueError, RuntimeError)) else type(exc).__name__
                        self._status("Camera error: repeated frame failure at {} ({}: {})".format(
                            failure_stage, type(exc).__name__, detail))
                        break
                    if failed_processing == 1:
                        detail = str(exc) if isinstance(exc, (InvalidCapturedFrame, ValueError, RuntimeError)) else type(exc).__name__
                        self._status("Camera frame failed at {} ({}); retrying (1/{})".format(
                            failure_stage, detail, self.processing_failure_limit))
                    # A valid flipped frame may still be previewed after an
                    # eye-processing failure; unsupported capture values are
                    # never sent to the dashboard.
                    if preview_frame is not None:
                        self._deliver_preview(preview_frame)
                    continue

                if failed_processing:
                    self._status("Camera frame processing recovered")
                failed_processing = 0
                if not connected:
                    self._status("Camera reconnected" if reconnect_cycles else "Camera connected")
                    connected = True
                self._deliver_preview(frame)
                self._run_calibration_request(controller)
        except Exception as exc:
            logger.exception("Camera worker failed")
            stage = getattr(exc, "pipeline_stage", "camera_worker")
            self._status("Camera error at {}: {}".format(stage, type(exc).__name__))
        finally:
            self._stop_event.set()
            self._calibration_requested.clear()
            self._release_capture(cap)
            with self._state_lock:
                if self.cap is cap:
                    self.cap = None
            if controller is None:
                with self._state_lock:
                    controller = self.eye_controller
            self._close_controller(controller)
            with self.frame_lock:
                self.latest_frame = None
            with self._state_lock:
                self.running = False
            if owns_device:
                device_lock.release()
            self._status("Camera stopped")

    def _run_calibration_request(self, controller):
        if not self._calibration_requested.is_set():
            return
        self._calibration_requested.clear()
        if controller is None:
            self._status("Eye controller is unavailable for calibration")
            return
        with self.frame_lock:
            frame = None if self.latest_frame is None else self.latest_frame.copy()
        if frame is None:
            self._status("Calibration skipped; no current camera frame")
            return
        try:
            controller.calibrate(frame)
        except Exception as exc:
            logger.exception("Camera calibration request failed")
            self._status("Calibration failed: {}".format(type(exc).__name__))

    @staticmethod
    def _log_frame_metadata(frame):
        """Log one metadata-only sample; never logs image pixels."""
        frame_type = "{}.{}".format(type(frame).__module__, type(frame).__name__)
        shape = getattr(frame, "shape", None)
        dtype = getattr(frame, "dtype", None)
        strides = getattr(frame, "strides", None)
        flags = getattr(frame, "flags", None)
        contiguous = getattr(flags, "c_contiguous", None)
        writable = getattr(flags, "writeable", None)
        owned = getattr(flags, "owndata", None)
        if shape is None or dtype is None:
            interface = getattr(frame, "__array_interface__", {})
            shape = interface.get("shape", shape)
            typestr = interface.get("typestr")
            dtype = typestr if typestr is not None else dtype
            strides = interface.get("strides", strides)
            data = interface.get("data")
            if isinstance(data, tuple) and len(data) > 1:
                writable = not bool(data[1])
        if contiguous is None and isinstance(shape, tuple) and dtype is not None:
            itemsize = np.dtype(dtype).itemsize
            expected = []
            stride = itemsize
            for dimension in reversed(shape):
                expected.append(stride)
                stride *= dimension
            expected = tuple(reversed(expected))
            contiguous = strides is None or tuple(strides) == expected
        logger.info("Camera frame metadata type=%s shape=%s channels=%s dtype=%s strides=%s contiguous=%s writable=%s owned=%s",
                    frame_type, shape, shape[2] if isinstance(shape, tuple) and len(shape) > 2 else None,
                    dtype, strides, contiguous, writable, owned)

    def calibrate(self):
        with self._state_lock:
            worker_alive = self.thread is not None and self.thread.is_alive()
            controller_available = self.eye_controller is not None or self.controller_factory is not None
            running = self.running
            if not running or not worker_alive or not controller_available:
                self._status("Start eye tracking before calibration")
                return False
            with self.frame_lock:
                has_frame = self.latest_frame is not None
            if not has_frame:
                self._status("Wait for a camera frame before calibration")
                return False
            self._calibration_requested.set()
        self._status("Calibration request queued on the camera worker")
        return True

    def _close_controller(self, controller):
        if controller is None:
            return
        try:
            controller.close()
        except Exception:
            logger.exception("Eye controller cleanup failed")
            self._status("Eye controller cleanup failed")
        finally:
            with self._state_lock:
                if self.eye_controller is controller:
                    self.eye_controller = None

    def _close_controller_without_worker(self):
        with self._state_lock:
            if self.thread is not None and self.thread.is_alive():
                return False
            controller = self.eye_controller
        self._close_controller(controller)
        return True

    def reset_controller(self):
        if not self.stop():
            return False
        with self._state_lock:
            self.controller_factory = None
        return self._close_controller_without_worker()

    def stop(self, timeout=None):
        timeout = self.stop_timeout if timeout is None else max(0.0, float(timeout))
        with self._state_lock:
            self.running = False
            self._stop_event.set()
            worker = self.thread
        if worker is None:
            return self._close_controller_without_worker()
        if worker is threading.current_thread():
            self._status("Camera stop requested from worker; cleanup will complete after this frame")
            return False
        if worker.is_alive():
            worker.join(timeout=timeout)
        if worker.is_alive():
            self._status("Camera stop timed out; worker still owns camera resources")
            return False
        return True
