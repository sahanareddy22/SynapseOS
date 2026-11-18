import cv2


class FaceAuthenticator:
    def __init__(self):
        self.face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    def authenticate(self, camera_index=0):
        camera = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
        if not camera.isOpened():
            camera.release()
            return None
        try:
            while True:
                ok, frame = camera.read()
                if not ok:
                    return None
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                if len(self.face_cascade.detectMultiScale(gray, 1.3, 5)):
                    return "User"
                cv2.imshow("Face Authentication · Q to cancel", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    return None
        finally:
            camera.release()
            cv2.destroyWindow("Face Authentication · Q to cancel")
