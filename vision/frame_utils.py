"""Canonical camera-frame conversion shared by capture and eye processing."""
import numpy as np


class FrameNormalizationError(ValueError):
    """Raised when a camera buffer cannot become a valid BGR image."""

    pipeline_stage = "frame_conversion"


class FrameFlipError(FrameNormalizationError):
    """Raised when a canonical BGR frame cannot be mirrored safely."""

    pipeline_stage = "horizontal_flip"


def validate_canonical_bgr_frame(frame):
    """Return frame only when it is an owned, writable canonical BGR ndarray."""
    if (type(frame) is not np.ndarray or frame.ndim != 3 or frame.shape[2] != 3
            or frame.shape[0] <= 0 or frame.shape[1] <= 0 or frame.dtype != np.uint8
            or not frame.flags.c_contiguous or not frame.flags.writeable
            or not frame.flags.owndata):
        raise FrameNormalizationError("Frame is not an owned canonical uint8 BGR ndarray")
    return frame


def normalize_bgr_frame(frame):
    """Return an owned, writable, C-contiguous active-NumPy uint8 BGR image."""
    is_native_array = isinstance(frame, np.ndarray)
    frame_type = type(frame)
    is_foreign_array = (
        not is_native_array
        and frame_type.__module__ == "numpy"
        and frame_type.__name__ == "ndarray"
        and hasattr(frame, "__array_interface__")
    )
    if not (is_native_array or is_foreign_array):
        raise FrameNormalizationError(
            "Camera frame must be a NumPy ndarray; received {}.{}".format(
                frame_type.__module__, frame_type.__name__
            )
        )

    try:
        if is_foreign_array:
            interface = frame.__array_interface__
            shape = tuple(interface["shape"])
            dtype = np.dtype(interface["typestr"])
            data = interface.get("data")
            if not data or not data[0]:
                raise FrameNormalizationError("Camera frame has no pixel data")
        else:
            shape = tuple(frame.shape)
            dtype = np.dtype(frame.dtype)
    except FrameNormalizationError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise FrameNormalizationError("Camera frame has invalid array metadata") from exc

    if len(shape) != 3 or shape[2] != 3:
        raise FrameNormalizationError("Camera frame must have shape (height, width, 3)")
    if shape[0] <= 0 or shape[1] <= 0:
        raise FrameNormalizationError("Camera frame dimensions must be non-empty")
    if dtype != np.uint8:
        raise FrameNormalizationError("Camera frame dtype must be uint8; got {}".format(dtype))

    try:
        canonical = np.array(frame, dtype=np.uint8, copy=True, order="C", subok=False)
    except (TypeError, ValueError, MemoryError) as exc:
        raise FrameNormalizationError("Camera frame could not be copied into active NumPy") from exc
    if canonical.shape != shape:
        raise FrameNormalizationError("Canonical BGR frame dimensions changed during conversion")
    return validate_canonical_bgr_frame(canonical)


def flip_bgr_horizontally(frame):
    """Mirror an already canonical BGR frame without crossing OpenCV's NumPy ABI."""
    try:
        validate_canonical_bgr_frame(frame)
    except FrameNormalizationError as exc:
        raise FrameFlipError("Horizontal flip received a non-canonical frame") from exc
    try:
        mirrored = np.ascontiguousarray(frame[:, ::-1, :])
    except (TypeError, ValueError, MemoryError) as exc:
        raise FrameFlipError("Could not mirror canonical camera frame") from exc
    if mirrored.shape != frame.shape:
        raise FrameFlipError("Mirrored BGR frame dimensions changed")
    try:
        return validate_canonical_bgr_frame(mirrored)
    except FrameNormalizationError as exc:
        raise FrameFlipError("Mirrored frame failed canonical validation") from exc
