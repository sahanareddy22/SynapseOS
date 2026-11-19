"""Thread-safe arbitration for voice and eye-generated desktop input."""
import logging
import threading


logger = logging.getLogger(__name__)

class DesktopInputBlocked(RuntimeError):
    """Raised when desktop input is rejected by cancellation or emergency stop."""


class DesktopInputCoordinator:
    """Serialize input transactions and give waiting voice actions priority.

    The condition lock protects coordinator state only; it is released while an
    action runs. Eye movement is never queued frame-by-frame: while voice owns
    or waits for input, only the latest gaze target is retained for diagnostics
    and the next live camera frame supplies the next executable target.
    """

    def __init__(self):
        self._condition = threading.Condition()
        self._active_source = None
        self._active_owner = None
        self._active_generation = None
        self._voice_waiters = 0
        self._cancel_generation = 0
        self._pending_gaze_target = None
        self._cleanup_pending = False
        self.emergency_latched = False
        self.eye_paused = False

    @property
    def pending_gaze_target(self):
        with self._condition:
            return self._pending_gaze_target

    @property
    def eye_actions_enabled(self):
        with self._condition:
            return not self.emergency_latched and not self.eye_paused

    @property
    def emergency_active(self):
        with self._condition:
            return self.emergency_latched

    def run_voice_transaction(self, action):
        with self._condition:
            if self.emergency_latched:
                raise DesktopInputBlocked("Emergency stop is active; desktop input was rejected.")
            generation = self._cancel_generation
            self._voice_waiters += 1
            try:
                while self._active_source is not None or self._cleanup_pending:
                    if self.emergency_latched:
                        raise DesktopInputBlocked("Emergency stop is active; desktop input was rejected.")
                    if generation != self._cancel_generation:
                        raise DesktopInputBlocked("Voice action was cancelled before execution.")
                    self._condition.wait()
                if self.emergency_latched:
                    raise DesktopInputBlocked("Emergency stop is active; desktop input was rejected.")
                if generation != self._cancel_generation:
                    raise DesktopInputBlocked("Voice action was cancelled before execution.")
                self._active_source = "voice"
                self._active_owner = threading.get_ident()
                self._active_generation = generation
            finally:
                self._voice_waiters -= 1

        try:
            return action()
        finally:
            with self._condition:
                if self._active_source == "voice" and self._active_owner == threading.get_ident():
                    self._active_source = None
                    self._active_owner = None
                    self._active_generation = None
                    self._condition.notify_all()

    def check_voice_action(self):
        with self._condition:
            if self._active_source != "voice" or self._active_owner != threading.get_ident():
                raise DesktopInputBlocked("Desktop input must run inside a voice transaction.")
            if self.emergency_latched:
                raise DesktopInputBlocked("Emergency stop interrupted the voice action.")
            if self._active_generation != self._cancel_generation:
                raise DesktopInputBlocked("Voice action was cancelled.")

    def check_mouse_release(self):
        with self._condition:
            if (self._active_owner != threading.get_ident()
                    or self._active_source not in {"voice", "cleanup"}):
                raise DesktopInputBlocked("Mouse release must run inside a coordinated transaction.")

    def current_thread_owns_voice_transaction(self):
        with self._condition:
            return self._active_source == "voice" and self._active_owner == threading.get_ident()

    def run_eye_action(self, kind, action, target=None):
        """Run a short eye action or suppress it when voice has priority."""
        if kind not in {"move", "click"}:
            raise ValueError("Eye input kind must be 'move' or 'click'.")
        with self._condition:
            if self.emergency_latched:
                self._pending_gaze_target = None
                logger.warning("COORDINATOR eye_action=rejected kind=%s reason=emergency_stop", kind)
                return False
            if self.eye_paused:
                self._pending_gaze_target = None
                logger.info("COORDINATOR eye_action=rejected kind=%s reason=eye_paused", kind)
                return False
            if self._active_source is not None or self._voice_waiters or self._cleanup_pending:
                if kind == "move":
                    self._pending_gaze_target = target
                reason = ("voice_priority" if self._active_source == "voice" or self._voice_waiters
                          else "input_transaction_active" if self._active_source is not None
                          else "cleanup_pending")
                logger.info("COORDINATOR eye_action=rejected kind=%s reason=%s", kind, reason)
                return False
            self._pending_gaze_target = None
            self._active_source = "eye"
            self._active_owner = threading.get_ident()

        try:
            logger.info("COORDINATOR eye_action=accepted kind=%s", kind)
            action()
            logger.info("COORDINATOR eye_action=completed kind=%s", kind)
            return True
        except Exception:
            logger.exception("COORDINATOR eye_action=failed kind=%s", kind)
            raise
        finally:
            with self._condition:
                if self._active_source == "eye" and self._active_owner == threading.get_ident():
                    self._active_source = None
                    self._active_owner = None
                    self._condition.notify_all()

    def cancel_voice_actions(self):
        """Invalidate active multi-step work at its next checked boundary."""
        with self._condition:
            self._cancel_generation += 1
            self._condition.notify_all()

    def activate_emergency(self):
        """Latch emergency before cancellation and reject all new desktop input."""
        with self._condition:
            self.emergency_latched = True
            self.eye_paused = True
            self._pending_gaze_target = None
            self._cancel_generation += 1
            self._condition.notify_all()

    def reset_emergency(self, *, confirmed=False):
        """Explicit reset hook; the dashboard's normal policy is process restart."""
        if not confirmed:
            return False
        with self._condition:
            if self._active_source is not None or self._voice_waiters:
                return False
            self.emergency_latched = False
            # Reset leaves eye actions paused until the user explicitly resumes.
            self.eye_paused = True
            self._pending_gaze_target = None
            self._cancel_generation += 1
            self._condition.notify_all()
            return True

    def pause_eye(self):
        with self._condition:
            if self.emergency_latched:
                return False
            self.eye_paused = True
            self._pending_gaze_target = None
            return True

    def resume_eye(self):
        with self._condition:
            if self.emergency_latched:
                return False
            self.eye_paused = False
            self._pending_gaze_target = None
            return True

    def schedule_cleanup(self, action):
        """Run safety cleanup after the current transaction, without blocking UI."""
        with self._condition:
            if self._cleanup_pending:
                return False
            self._cleanup_pending = True

        def cleanup_worker():
            try:
                with self._condition:
                    while self._active_source is not None:
                        self._condition.wait()
                    self._active_source = "cleanup"
                    self._active_owner = threading.get_ident()
                action()
            finally:
                with self._condition:
                    self._active_source = None
                    self._active_owner = None
                    self._cleanup_pending = False
                    self._condition.notify_all()

        threading.Thread(target=cleanup_worker, name="SynapseInputCleanup", daemon=True).start()
        return True


# Standalone adapters/controllers share one process-wide coordinator. The
# dashboard also passes this object explicitly to both components.
DEFAULT_INPUT_COORDINATOR = DesktopInputCoordinator()
