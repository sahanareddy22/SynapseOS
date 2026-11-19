"""Cancellable one-shot request from the voice worker to the GUI thread."""
import threading


class VoiceActionRequest:
    """A queued GUI callback may claim a request only while it is pending."""
    def __init__(self, intent):
        self.intent = intent
        self.event = threading.Event()
        self.result = {}
        self._lock = threading.Lock()
        self._state = "pending"

    def claim(self):
        with self._lock:
            if self._state != "pending":
                return False
            self._state = "running"
            return True

    def cancel_if_pending(self):
        with self._lock:
            if self._state != "pending":
                return False
            self._state = "cancelled"
            self.result["value"] = {"status": "cancelled", "message": "Queued action cancelled before execution."}
            self.event.set()
            return True

    def wait(self, timeout):
        if self.event.wait(timeout):
            return False
        if self.cancel_if_pending():
            return True
        # Once claimed, allow the operation to return its result. Reporting a
        # timeout while it may still complete can cause an unsafe duplicate retry.
        self.event.wait()
        return False

    def complete(self, value=None, exception=None):
        with self._lock:
            if self._state != "running":
                return False
            self.result["value"] = value
            if exception is not None:
                self.result["exception"] = exception
            self._state = "completed"
            self.event.set()
            return True
