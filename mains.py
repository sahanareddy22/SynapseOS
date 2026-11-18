"""Standalone eye tracker entry point; press C to calibrate and Q to quit."""
import cv2

from vision.camera_pipeline import CameraPipeline
from vision.eye_control import EyeController


def main():
    pipeline = CameraPipeline(EyeController(), on_frame=lambda frame: cv2.imshow("SynapseOS eye tracking", frame))
    pipeline.start()
    try:
        while pipeline.running:
            key = cv2.waitKey(20) & 0xFF
            if key == ord("q"):
                break
            if key == ord("c"):
                pipeline.calibrate()
    finally:
        pipeline.stop()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
