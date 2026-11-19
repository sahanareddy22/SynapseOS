"""Hardware-free lifecycle tests for CameraPipeline."""
import importlib
import sys
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np


class FakeFrame(np.ndarray):
    def __new__(cls, value=0):
        return np.full((24, 32, 3), value, dtype=np.uint8).view(cls)


class ForeignNumpyArray:
    """Models an ndarray class from a different loaded NumPy C-API instance."""

    def __init__(self, array):
        self.array = array

    @property
    def __array_interface__(self):
        return self.array.__array_interface__


ForeignNumpyArray.__name__ = "ndarray"
ForeignNumpyArray.__qualname__ = "ndarray"
ForeignNumpyArray.__module__ = "numpy"


def load_pipeline():
    fake_cv2 = types.ModuleType("cv2")
    fake_cv2.CAP_DSHOW = 700
    fake_cv2.Mat = FakeFrame
    fake_cv2.error = RuntimeError
    fake_cv2.flip = Mock(side_effect=RuntimeError("OpenCV ndarray bridge must not be used"))
    fake_cv2.VideoCapture = Mock()
    with patch.dict(sys.modules, {"cv2": fake_cv2}):
        sys.modules.pop("vision.camera_pipeline", None)
        module = importlib.import_module("vision.camera_pipeline")
    return module


pipeline_module = load_pipeline()


class CaptureFrameContractTests(unittest.TestCase):
    def test_valid_numpy_bgr_frame_is_canonical_owned_copy(self):
        frame = np.zeros((4, 6, 3), dtype=np.uint8)
        normalized = pipeline_module._validate_capture_frame(frame)
        self.assertIs(type(normalized), np.ndarray)
        self.assertIsNot(normalized, frame)
        self.assertTrue(normalized.flags.c_contiguous)
        self.assertTrue(normalized.flags.writeable)

    def test_foreign_numpy_ndarray_is_copied_into_active_numpy(self):
        backing = np.full((5, 7, 3), 41, dtype=np.uint8)
        frame = ForeignNumpyArray(backing)

        validated = pipeline_module._validate_capture_frame(frame)

        self.assertIs(type(validated), np.ndarray)
        self.assertIsNot(validated, frame)
        self.assertEqual(validated.shape, (5, 7, 3))
        self.assertEqual(validated.dtype, np.uint8)
        self.assertTrue(validated.flags.c_contiguous)
        self.assertTrue(validated.flags.writeable)

    def test_invalid_frame_shapes_types_and_dtypes_are_rejected(self):
        invalid_frames = (
            None,
            object(),
            np.zeros((0, 6, 3), dtype=np.uint8),
            np.zeros((4, 6), dtype=np.uint8),
            np.zeros((4, 6, 4), dtype=np.uint8),
            np.zeros((4, 6, 3), dtype=np.float32),
        )
        for frame in invalid_frames:
            with self.subTest(frame_type=type(frame).__name__, shape=getattr(frame, "shape", None)):
                with self.assertRaises(pipeline_module.InvalidCapturedFrame):
                    pipeline_module._validate_capture_frame(frame)

    def test_non_contiguous_input_is_normalized_for_opencv(self):
        frame = np.zeros((6, 12, 3), dtype=np.uint8)[:, ::2]
        self.assertFalse(frame.flags.c_contiguous)

        validated = pipeline_module._validate_capture_frame(frame)

        self.assertIsNot(validated, frame)
        self.assertTrue(validated.flags.c_contiguous)
        self.assertEqual(validated.shape, (6, 6, 3))

    def test_read_only_capture_buffer_becomes_writable_owned_array(self):
        frame = np.zeros((5, 7, 3), dtype=np.uint8)
        frame.setflags(write=False)

        normalized = pipeline_module._validate_capture_frame(frame)

        self.assertTrue(normalized.flags.writeable)
        self.assertTrue(normalized.flags.owndata)
        self.assertTrue(normalized.flags.c_contiguous)


class FakeCapture:
    def __init__(self, opened=True, reads=None, release_hook=None):
        self.opened = opened
        self.reads = list(reads or [])
        self.release_hook = release_hook
        self.release_count = 0

    def isOpened(self):
        return self.opened

    def read(self):
        if self.reads:
            result = self.reads.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return False, None

    def release(self):
        self.release_count += 1
        if self.release_hook:
            self.release_hook()


class FakeController:
    def __init__(self, on_process=None):
        self.on_process = on_process
        self.process_threads = []
        self.processed_frames = []
        self.calibration_threads = []
        self.close_count = 0
        self.face_mesh = Mock()

    def process_frame(self, frame):
        self.process_threads.append(threading.get_ident())
        self.processed_frames.append(frame)
        if self.on_process:
            self.on_process()
        return frame

    def calibrate(self, frame):
        self.calibration_threads.append(threading.get_ident())
        return True

    def close(self):
        self.close_count += 1
        self.face_mesh.close()


class CameraPipelineTests(unittest.TestCase):
    def setUp(self):
        self.capture_factory = Mock()
        self.patcher = patch.object(pipeline_module.cv2, "VideoCapture", self.capture_factory)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    @staticmethod
    def wait_for(predicate, timeout=1.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def make_pipeline(self, controller=None, **kwargs):
        kwargs.setdefault("retry_interval", 0)
        kwargs.setdefault("stop_timeout", 0.5)
        return pipeline_module.CameraPipeline(
            eye_controller=controller,
            on_status=kwargs.pop("on_status", Mock()),
            **kwargs,
        )

    def test_successful_startup_and_cleanup(self):
        frame = FakeFrame(value=17)
        cap = FakeCapture(reads=[(True, frame)])
        controller = FakeController()
        self.capture_factory.return_value = cap
        received = threading.Event()
        pipeline = self.make_pipeline(controller, on_frame=lambda _frame: received.set(), max_reconnect_cycles=0)
        self.assertTrue(pipeline.start())
        self.assertTrue(received.wait(1))
        self.assertTrue(self.wait_for(lambda: not pipeline.running))
        self.assertEqual(len(controller.processed_frames), 1)
        processed = controller.processed_frames[0]
        self.assertIsInstance(processed, np.ndarray)
        self.assertEqual(processed.shape, (24, 32, 3))
        self.assertEqual(processed.dtype, np.uint8)
        self.assertTrue(processed.flags.c_contiguous)
        self.assertTrue(np.all(processed == 17))
        self.assertEqual(cap.release_count, 1)
        self.assertEqual(controller.close_count, 1)

    def test_capture_ret_frame_tuple_is_unpacked_and_numpy_frame_reaches_controller(self):
        frame = FakeFrame(value=93)
        cap = FakeCapture(reads=[(True, frame)])
        controller = FakeController()
        self.capture_factory.return_value = cap
        pipeline = self.make_pipeline(controller, max_reconnect_cycles=0)

        self.assertTrue(pipeline.start())
        self.assertTrue(self.wait_for(lambda: not pipeline.running))

        self.assertEqual(len(controller.processed_frames), 1)
        self.assertIsInstance(controller.processed_frames[0], np.ndarray)
        self.assertTrue(np.all(controller.processed_frames[0] == 93))

    def test_capture_is_normalized_before_horizontal_flip(self):
        capture = FakeFrame(value=38)
        cap = FakeCapture(reads=[(True, capture), (False, None)])
        controller = FakeController()
        self.capture_factory.return_value = cap
        observed = []
        original_flip = pipeline_module.flip_bgr_horizontally
        def inspect_flip(frame):
            observed.append((type(frame), frame.flags.c_contiguous,
                             frame.flags.writeable, frame.flags.owndata))
            return original_flip(frame)
        pipeline = self.make_pipeline(controller, read_failure_limit=1)

        with patch.object(pipeline_module, "flip_bgr_horizontally", side_effect=inspect_flip):
            self.assertTrue(pipeline.start())
            self.assertTrue(self.wait_for(lambda: not pipeline.running))

        self.assertEqual(observed, [(np.ndarray, True, True, True)])
        pipeline_module.cv2.flip.assert_not_called()

    def test_multiple_valid_capture_frames_reach_preview_continuously(self):
        cap = FakeCapture(reads=[(True, FakeFrame(value=14)), (True, FakeFrame(value=28)), (False, None)])
        controller = FakeController()
        previews = []
        self.capture_factory.return_value = cap
        pipeline = self.make_pipeline(controller, on_frame=previews.append,
                                      read_failure_limit=1, max_reconnect_cycles=0)

        self.assertTrue(pipeline.start())
        self.assertTrue(self.wait_for(lambda: not pipeline.running))

        self.assertEqual(len(previews), 2)
        self.assertEqual([int(frame[0, 0, 0]) for frame in previews], [14, 28])

    def test_transient_mediapipe_failure_does_not_corrupt_next_preview_frame(self):
        cap = FakeCapture(reads=[(True, FakeFrame(value=12)), (True, FakeFrame(value=24)), (False, None)])
        calls = []
        def process():
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("transient MediaPipe failure")
        controller = FakeController(on_process=process)
        previews = []
        self.capture_factory.return_value = cap
        pipeline = self.make_pipeline(controller, on_frame=previews.append,
                                      read_failure_limit=1, max_reconnect_cycles=0)

        self.assertTrue(pipeline.start())
        self.assertTrue(self.wait_for(lambda: not pipeline.running))

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(controller.processed_frames), 2)
        self.assertGreaterEqual(len(previews), 2)
        self.assertEqual(int(previews[-1][0, 0, 0]), 24)

    def test_preview_callback_failure_does_not_stop_frame_processing(self):
        cap = FakeCapture(reads=[(True, FakeFrame(value=1)), (True, FakeFrame(value=2)), (False, None)])
        controller = FakeController()
        statuses = []
        self.capture_factory.return_value = cap
        preview = Mock(side_effect=RuntimeError("mock Qt callback failure"))
        pipeline = self.make_pipeline(controller, on_frame=preview, on_status=statuses.append,
                                      read_failure_limit=1, max_reconnect_cycles=0)

        self.assertTrue(pipeline.start())
        self.assertTrue(self.wait_for(lambda: not pipeline.running))

        self.assertEqual(len(controller.processed_frames), 2)
        self.assertEqual(preview.call_count, 2)
        self.assertEqual(sum("Camera preview error" in item for item in statuses), 1)
        self.assertEqual(cap.release_count, 1)

    def test_annotated_controller_output_is_forwarded_to_preview_callback(self):
        captured = FakeFrame(value=10)
        annotated = np.full((24, 32, 3), 77, dtype=np.uint8)
        cap = FakeCapture(reads=[(True, captured), (False, None)])
        controller = FakeController()
        controller.process_frame = Mock(return_value=annotated)
        preview_frames = []
        self.capture_factory.return_value = cap
        pipeline = self.make_pipeline(controller, on_frame=preview_frames.append, read_failure_limit=1)

        self.assertTrue(pipeline.start())
        self.assertTrue(self.wait_for(lambda: not pipeline.running))

        self.assertGreaterEqual(len(preview_frames), 1)
        self.assertIs(type(preview_frames[0]), np.ndarray)
        self.assertTrue(np.all(preview_frames[0] == 77))

    def test_invalid_capture_frame_recovers_on_next_valid_frame(self):
        cap = FakeCapture(reads=[(True, object()), (True, FakeFrame(value=8)), (False, None)])
        controller = FakeController()
        statuses = []
        self.capture_factory.return_value = cap
        pipeline = self.make_pipeline(
            controller, on_status=statuses.append, read_failure_limit=1,
            max_reconnect_cycles=0,
        )

        self.assertTrue(pipeline.start())
        self.assertTrue(self.wait_for(lambda: not pipeline.running))

        self.assertEqual(len(controller.processed_frames), 1)
        self.assertTrue(np.all(controller.processed_frames[0] == 8))
        self.assertEqual(controller.close_count, 1)
        self.assertEqual(cap.release_count, 1)
        self.assertIn("Camera frame processing recovered", statuses)
        self.assertIn("Camera connected", statuses)

    def test_preprocessing_and_mediapipe_failures_report_distinct_stages(self):
        for stage in ("image_preprocessing", "mediapipe_processing"):
            with self.subTest(stage=stage):
                cap = FakeCapture(reads=[(True, FakeFrame(value=3))])
                controller = FakeController()
                statuses = []
                self.capture_factory.return_value = cap

                class StageError(RuntimeError):
                    pipeline_stage = stage

                def fail_processing():
                    raise StageError("mock {} failure".format(stage))

                controller.on_process = fail_processing
                pipeline = self.make_pipeline(
                    controller, on_status=statuses.append, processing_failure_limit=1,
                )
                self.assertTrue(pipeline.start())
                self.assertTrue(self.wait_for(lambda: not pipeline.running))
                self.assertTrue(any("Camera error: repeated frame failure at {}".format(stage) in status
                                    for status in statuses))
                self.assertEqual(controller.close_count, 1)
                self.assertEqual(cap.release_count, 1)

    def test_invalid_capture_frame_type_fails_clearly_and_releases_resources(self):
        cap = FakeCapture(reads=[(True, object()), (False, None)])
        controller = FakeController()
        statuses = []
        self.capture_factory.return_value = cap
        pipeline = self.make_pipeline(
            controller, on_status=statuses.append, processing_failure_limit=1,
        )

        self.assertTrue(pipeline.start())
        self.assertTrue(self.wait_for(lambda: not pipeline.running))

        self.assertEqual(controller.processed_frames, [])
        self.assertEqual(controller.close_count, 1)
        self.assertEqual(cap.release_count, 1)
        self.assertTrue(any("Camera error: repeated frame failure at capture_validation" in status
                            for status in statuses))
        self.assertNotIn("Camera connected", statuses)

    def test_failed_read_does_not_pass_frame_to_controller_and_cleans_up(self):
        cap = FakeCapture(reads=[(False, None)])
        controller = FakeController()
        self.capture_factory.return_value = cap
        pipeline = self.make_pipeline(
            controller, read_failure_limit=1, max_reconnect_cycles=0,
        )

        self.assertTrue(pipeline.start())
        self.assertTrue(self.wait_for(lambda: not pipeline.running))

        self.assertEqual(controller.processed_frames, [])
        self.assertEqual(controller.close_count, 1)
        self.assertEqual(cap.release_count, 1)

    def test_camera_unavailable_releases_capture_and_controller(self):
        cap = FakeCapture(opened=False)
        self.capture_factory.return_value = cap
        controller = FakeController()
        pipeline = self.make_pipeline(controller, reconnect_attempts=2)
        self.assertTrue(pipeline.start())
        self.assertTrue(self.wait_for(lambda: not pipeline.running))
        self.assertEqual(self.capture_factory.call_count, 2)
        self.assertEqual(cap.release_count, 2)
        self.assertEqual(controller.close_count, 1)

    def test_repeated_read_failures_are_bounded(self):
        cap = FakeCapture(reads=[(False, None)] * 10)
        self.capture_factory.return_value = cap
        pipeline = self.make_pipeline(FakeController(), read_failure_limit=2, max_reconnect_cycles=0)
        pipeline.start()
        self.assertTrue(self.wait_for(lambda: not pipeline.running))
        self.assertEqual(self.capture_factory.call_count, 1)
        self.assertEqual(cap.release_count, 1)

    def test_single_processing_failure_is_reported_and_next_frame_recovers(self):
        cap = FakeCapture(reads=[(True, FakeFrame()), (True, FakeFrame()), (False, None)])
        self.capture_factory.return_value = cap
        statuses = []

        class FailOnceController(FakeController):
            def process_frame(self, frame):
                self.process_threads.append(threading.get_ident())
                if len(self.process_threads) == 1:
                    raise TypeError("mock MediaPipe frame failure")
                return frame

        controller = FailOnceController()
        pipeline = self.make_pipeline(
            controller, on_status=statuses.append, read_failure_limit=1,
            max_reconnect_cycles=0,
        )
        self.assertTrue(pipeline.start())
        self.assertTrue(self.wait_for(lambda: not pipeline.running))

        self.assertEqual(len(controller.process_threads), 2)
        self.assertEqual(controller.close_count, 1)
        self.assertEqual(cap.release_count, 1)
        self.assertIn("Camera frame failed at eye_processing (TypeError); retrying (1/5)", statuses)
        self.assertIn("Camera frame processing recovered", statuses)

    def test_repeated_processing_failure_reports_error_and_releases_resources(self):
        cap = FakeCapture(reads=[(True, FakeFrame()), (True, FakeFrame()), (True, FakeFrame())])
        self.capture_factory.return_value = cap
        statuses = []

        class AlwaysFailController(FakeController):
            def process_frame(self, frame):
                self.process_threads.append(threading.get_ident())
                raise TypeError("mock persistent MediaPipe failure")

        controller = AlwaysFailController()
        pipeline = self.make_pipeline(
            controller, on_status=statuses.append, processing_failure_limit=2,
        )
        self.assertTrue(pipeline.start())
        self.assertTrue(self.wait_for(lambda: not pipeline.running))

        self.assertEqual(len(controller.process_threads), 2)
        self.assertEqual(controller.close_count, 1)
        self.assertEqual(cap.release_count, 1)
        self.assertTrue(any(status.startswith("Camera error: repeated frame failure at eye_processing (TypeError:")
                            for status in statuses))

    def test_camera_recovers_after_disconnect_and_reports_only_after_frame(self):
        first = FakeCapture(reads=[(False, None)])
        second = FakeCapture(reads=[(True, FakeFrame())])
        self.capture_factory.side_effect = [first, second]
        statuses = []
        pipeline = self.make_pipeline(
            FakeController(), on_status=statuses.append,
            read_failure_limit=1, max_reconnect_cycles=1,
        )
        pipeline.start()
        self.assertTrue(self.wait_for(lambda: not pipeline.running))
        self.assertEqual(first.release_count, 1)
        self.assertEqual(second.release_count, 1)
        self.assertIn("Camera reconnected", statuses)

    def test_reconnect_cycle_limit_is_cumulative(self):
        captures = [
            FakeCapture(reads=[(True, FakeFrame()), (False, None)]),
            FakeCapture(reads=[(True, FakeFrame()), (False, None)]),
            FakeCapture(reads=[(True, FakeFrame())]),
        ]
        self.capture_factory.side_effect = captures
        pipeline = self.make_pipeline(
            FakeController(), read_failure_limit=1, max_reconnect_cycles=1,
        )
        pipeline.start()
        self.assertTrue(self.wait_for(lambda: not pipeline.running))
        self.assertEqual(self.capture_factory.call_count, 2)
        self.assertEqual([cap.release_count for cap in captures], [1, 1, 0])

    def test_stop_timeout_keeps_resources_owned_until_worker_exits(self):
        entered = threading.Event()
        unblock = threading.Event()

        class BlockingController(FakeController):
            def process_frame(inner_self, frame):
                entered.set()
                unblock.wait(1)
                return super(BlockingController, inner_self).process_frame(frame)

        cap = FakeCapture(reads=[(True, FakeFrame())])
        controller = BlockingController()
        self.capture_factory.return_value = cap
        pipeline = self.make_pipeline(controller, stop_timeout=0.01)
        pipeline.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(pipeline.stop(timeout=0.01))
        self.assertEqual(cap.release_count, 0)
        self.assertEqual(controller.close_count, 0)
        self.assertFalse(pipeline.start())
        unblock.set()
        self.assertTrue(pipeline.stop(timeout=1))
        self.assertEqual(cap.release_count, 1)
        self.assertEqual(controller.close_count, 1)

    def test_start_while_alive_is_rejected(self):
        entered = threading.Event()
        unblock = threading.Event()

        class BlockingController(FakeController):
            def process_frame(inner_self, frame):
                entered.set()
                unblock.wait(1)
                return frame

        cap = FakeCapture(reads=[(True, FakeFrame())])
        self.capture_factory.return_value = cap
        pipeline = self.make_pipeline(BlockingController())
        self.assertTrue(pipeline.start())
        self.assertTrue(entered.wait(1))
        old_worker = pipeline.thread
        self.assertFalse(pipeline.start())
        self.assertIs(pipeline.thread, old_worker)
        unblock.set()
        self.assertTrue(pipeline.stop(timeout=1))
        self.assertEqual(self.capture_factory.call_count, 1)

    def test_stop_then_restart_creates_serial_workers_and_closes_each_controller(self):
        captures = [FakeCapture(reads=[(True, FakeFrame())]), FakeCapture(reads=[(True, FakeFrame())])]
        controllers = [FakeController(), FakeController()]
        self.capture_factory.side_effect = captures
        factory = Mock(side_effect=controllers)
        pipeline = self.make_pipeline(controller_factory=factory, max_reconnect_cycles=0)
        self.assertTrue(pipeline.start())
        self.assertTrue(self.wait_for(lambda: not pipeline.running))
        self.assertTrue(pipeline.start())
        self.assertTrue(self.wait_for(lambda: not pipeline.running))
        self.assertEqual(factory.call_count, 2)
        self.assertEqual([c.close_count for c in controllers], [1, 1])
        self.assertEqual([c.release_count for c in captures], [1, 1])

    def test_repeated_stop_is_idempotent(self):
        cap = FakeCapture(reads=[(True, FakeFrame())])
        controller = FakeController()
        self.capture_factory.return_value = cap
        pipeline = self.make_pipeline(controller, max_reconnect_cycles=0)
        pipeline.start()
        self.assertTrue(self.wait_for(lambda: not pipeline.running))
        self.assertTrue(pipeline.stop())
        self.assertTrue(pipeline.stop())
        self.assertEqual(cap.release_count, 1)
        self.assertEqual(controller.close_count, 1)
        controller.face_mesh.close.assert_called_once_with()

    def test_calibration_is_run_on_the_camera_worker(self):
        pipeline_ref = {}
        queued = threading.Event()

        def request_calibration():
            pipeline_ref["pipeline"].calibrate()
            queued.set()

        controller = FakeController(on_process=request_calibration)
        cap = FakeCapture(reads=[(True, FakeFrame())])
        self.capture_factory.return_value = cap
        pipeline = self.make_pipeline(controller, max_reconnect_cycles=0)
        pipeline_ref["pipeline"] = pipeline
        pipeline.start()
        self.assertTrue(self.wait_for(lambda: bool(controller.calibration_threads)))
        self.assertTrue(queued.is_set())
        self.assertEqual(controller.calibration_threads[0], controller.process_threads[0])
        self.assertTrue(pipeline.stop())

    def test_global_device_lock_prevents_overlapping_pipeline_owners(self):
        entered = threading.Event()
        unblock = threading.Event()

        class BlockingController(FakeController):
            def process_frame(inner_self, frame):
                entered.set()
                unblock.wait(1)
                return frame

        first_cap = FakeCapture(reads=[(True, FakeFrame())])
        second_cap = FakeCapture(reads=[(True, FakeFrame())])
        self.capture_factory.side_effect = [first_cap, second_cap]
        first = self.make_pipeline(BlockingController(), camera_index=7123)
        second = self.make_pipeline(FakeController(), camera_index=7123, max_reconnect_cycles=0)
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        time.sleep(0.05)
        self.assertEqual(self.capture_factory.call_count, 1)
        unblock.set()
        self.assertTrue(first.stop(timeout=1))
        self.assertTrue(self.wait_for(lambda: self.capture_factory.call_count >= 2))
        self.assertTrue(self.wait_for(lambda: not second.running))
        self.assertEqual(first_cap.release_count, 1)
        self.assertEqual(second_cap.release_count, 1)


if __name__ == "__main__":
    unittest.main()
