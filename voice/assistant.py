"""Speech recognition/TTS orchestration for the catalog-based voice system."""
import queue
import logging
import os
import re
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pyttsx3
import speech_recognition as sr

from voice.dispatcher import CommandDispatcher


class TTS:
    """Thread-safe speech output kept on its own worker thread."""
    def __init__(self):
        self.speech_queue = queue.Queue(maxsize=1)
        self.ready = threading.Event()
        self.engine_error = None
        self.worker = threading.Thread(target=self._worker, name="SynapseTTS", daemon=True)
        self.worker.start()
        self.ready.wait(timeout=15)
        if not self.ready.is_set(): self.engine_error = RuntimeError("TTS initialization timed out")

    def _worker(self):
        engine = None
        try:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except ImportError:
                pythoncom = None
            try: engine = pyttsx3.init(driverName="sapi5")
            except Exception: engine = pyttsx3.init()
            engine.setProperty("rate", 165)
            engine.setProperty("volume", 1.0)
        except Exception as exc:
            self.engine_error = exc
            logging.getLogger(__name__).error("RESULT tts_initialization=failed error_type=%s", type(exc).__name__)
        finally:
            self.ready.set()
        if engine is None: return
        while True:
            message = self.speech_queue.get()
            try:
                if message is None: break
                engine.say(str(message)); engine.runAndWait()
            except Exception as exc:
                logging.getLogger(__name__).error("RESULT tts_playback=failed error_type=%s", type(exc).__name__)
            finally:
                self.speech_queue.task_done()
        try: engine.stop()
        except Exception: pass
        try:
            if pythoncom: pythoncom.CoUninitialize()
        except Exception: pass

    def speak(self, text):
        if not text or self.engine_error: return
        # Keep only the newest pending utterance; stale confirmations/statuses
        # should not be spoken after the user has already moved on.
        while True:
            try:
                self.speech_queue.get_nowait()
            except Exception as exc:
                if type(exc).__name__ != "Empty":
                    logging.getLogger(__name__).error("RESULT tts_queue=drain_failed error_type=%s", type(exc).__name__)
                break
            self.speech_queue.task_done()
        try: self.speech_queue.put_nowait(str(text))
        except queue.Full: pass

    def shutdown(self):
        if self.worker.is_alive():
            while True:
                try:
                    self.speech_queue.get_nowait()
                except Exception as exc:
                    if type(exc).__name__ != "Empty":
                        logging.getLogger(__name__).error("RESULT tts_queue=shutdown_drain_failed error_type=%s", type(exc).__name__)
                    break
                self.speech_queue.task_done()
            try: self.speech_queue.put_nowait(None)
            except queue.Full: pass


tts = TTS()
logger = logging.getLogger(__name__)


def configure_voice_diagnostics():
    """Install a rotating, privacy-filtered voice log under the user's local data folder."""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    log_path = root / "SynapseOS" / "logs" / "voice-diagnostics.log"
    voice_logger = logging.getLogger("voice")
    try:
        handler = next((h for h in voice_logger.handlers
                        if getattr(h, "_synapse_voice_diagnostics", False)), None)
        if handler is None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(str(log_path), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            handler._synapse_voice_diagnostics = True
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            voice_logger.addHandler(handler)
        for name in ("voice", "vision.camera_pipeline", "vision.eye_control",
                     "desktop_input", "vkeyboard"):
            target_logger = logging.getLogger(name)
            if not any(getattr(existing, "_synapse_voice_diagnostics", False)
                       for existing in target_logger.handlers):
                target_logger.addHandler(handler)
            target_logger.setLevel(logging.INFO)
            target_logger.propagate = False
    except OSError as exc:
        logger.warning("RESULT diagnostics_file=unavailable error_type=%s", type(exc).__name__)
        return None
    return str(log_path)


def _safe_recognition_error(exc):
    """Return bounded transport detail without URLs, control chars, or credentials."""
    detail = str(exc)
    detail = re.sub(r"https?://\S+", "<url>", detail, flags=re.IGNORECASE)
    detail = re.sub(r"(?i)(authorization|token|api[_-]?key|password)(\s*[=:]\s*)[^\s,;]+",
                    r"\1\2<redacted>", detail)
    detail = re.sub(r"[\r\n\t]+", " ", detail).strip()
    return detail[:240] or type(exc).__name__


def speak(text):
    tts.speak(text)


class VoiceAssistant:
    def __init__(self, wake_word="chitti", on_status=None, action_executor=None, cancel_executor=None,
                 recognizer=None, microphone=None, speech_output=speak):
        self.recognizer = recognizer or sr.Recognizer()
        self.microphone_error_type = None
        try: self.mic = microphone if microphone is not None else sr.Microphone()
        except Exception as exc:
            self.mic = None
            self.microphone_error_type = type(exc).__name__
        self.wake_word = wake_word.casefold()
        self.on_status = on_status or (lambda _message: None)
        self.speech_output = speech_output
        self.running = False
        self._stopping = False
        self.worker = None
        self._lifecycle_lock = threading.Lock()
        self.last_transcript = None
        self.last_command = None
        self.dispatcher = CommandDispatcher(action_executor or self._unconfigured_action,
                                            feedback=self._feedback,
                                            cancel_executor=cancel_executor)
        self.recognizer.energy_threshold = 200
        self.recognizer.dynamic_energy_threshold = True
        self.recognizer.dynamic_energy_adjustment_damping = 0.15
        self.recognizer.dynamic_energy_ratio = 1.5
        self.recognizer.pause_threshold = 0.8
        self.recognizer.phrase_threshold = 0.3
        self.recognizer.non_speaking_duration = 0.5

    @staticmethod
    def _unconfigured_action(_intent):
        return {"status": "unsupported", "message": "No action adapter is configured."}

    def _feedback(self, message):
        self.on_status(message)
        self.speech_output(message)

    def start(self, background=True):
        log_path = configure_voice_diagnostics()
        if log_path:
            logger.info("RESULT diagnostics_file=ready path=%s", log_path)
        if tts.engine_error:
            logger.error("RESULT tts=unavailable error_type=%s", type(tts.engine_error).__name__)
        if self.mic is None:
            logger.error("ASR microphone=unavailable error_type=%s", self.microphone_error_type or "Unavailable")
            self._feedback("Microphone is unavailable.")
            return
        with self._lifecycle_lock:
            if self.worker and self.worker.is_alive():
                if self._stopping or not self.running:
                    self.on_status("Voice listener is still stopping; try starting again shortly.")
                else:
                    self.dispatcher.listening_enabled = True
                    self.on_status("Voice listening resumed.")
                return
            self.running = True
            self._stopping = False
            self.dispatcher.listening_enabled = True
            self.on_status("Voice listener starting.")
            if background:
                self.worker = threading.Thread(target=self._listen_loop, name="SynapseVoice", daemon=True)
                self.worker.start()
                return
        self._listen_loop()

    def stop(self, wait=True):
        with self._lifecycle_lock:
            self.running = False
            self._stopping = True
        self.dispatcher.listening_enabled = False
        self.dispatcher.cancel_all()
        if wait and self.worker and self.worker.is_alive() and threading.current_thread() is not self.worker:
            self.worker.join(timeout=2)
        self.on_status("Voice listener stopped.")

    def emergency_stop(self, wait=True):
        self.dispatcher.emergency_stopped = True
        self.dispatcher.cancel_all()
        self.stop(wait=wait)

    def _listen_loop(self):
        if self.mic is None: return
        try:
            with self.mic as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=2)
                self.on_status("Listening. Say Chitti to wake voice control.")
                while self.running:
                    stage = "capture"
                    try:
                        self.dispatcher.expire_pending()
                        audio = self.recognizer.listen(source, timeout=3, phrase_time_limit=7)
                        logger.info("ASR capture=result_received")
                        stage = "recognition"
                        transcript = self.recognizer.recognize_google(audio)
                        self.last_transcript = transcript
                        logger.info("ASR result=transcript_received chars=%d", len(transcript))
                        self.on_status("Heard: {}".format(transcript))
                        command, wake_seen = self._remove_wake_word(transcript)
                        logger.info("WAKE detected=%s post_wake_chars=%d", wake_seen, len(command))
                        if not self.dispatcher.listening_enabled:
                            if not wake_seen:
                                logger.info("DISPATCHER result=ignored reason=wake_word_required")
                                continue
                            if not self._is_resume_phrase(command):
                                logger.info("DISPATCHER result=ignored reason=not_a_resume_or_emergency_command")
                                continue
                        if command.strip(): self._handle(command, transcript=transcript)
                        elif wake_seen:
                            logger.info("RESULT feedback_message=%r", "Yes?")
                            self._feedback("Yes?")
                    except sr.WaitTimeoutError:
                        self.dispatcher.expire_pending()
                        continue
                    except sr.UnknownValueError:
                        logger.warning("ASR result=unintelligible_audio")
                        self.on_status("Speech was not understood.")
                    except sr.RequestError as exc:
                        detail = _safe_recognition_error(exc)
                        if "connection failed" in detail.casefold():
                            category = "network_connection"
                            feedback = "Could not connect to Google speech recognition: {}".format(detail)
                        elif "timed out" in detail.casefold() or "timeout" in detail.casefold():
                            category = "network_timeout"
                            feedback = "Google speech recognition timed out: {}".format(detail)
                        elif "request failed" in detail.casefold():
                            category = "service_request"
                            feedback = "Google speech recognition request failed: {}".format(detail)
                        else:
                            category = "service_error"
                            feedback = "Google speech recognition error: {}".format(detail)
                        logger.error("ASR result=service_error category=%s error_type=%s detail=%r",
                                     category, type(exc).__name__, detail)
                        self._feedback(feedback)
                        time.sleep(2)
                    except Exception as exc:
                        stage_name = "MICROPHONE" if stage == "capture" else "ASR"
                        logger.error("%s result=error stage=%s error_type=%s", stage_name, stage, type(exc).__name__)
                        self.on_status("Microphone stream failed; restart voice control." if stage == "capture" else "Speech recognition failed.")
                        if stage == "capture":
                            break
        except Exception as exc:
            logger.error("ASR result=microphone_setup_error error_type=%s", type(exc).__name__)
            self._feedback("Could not start microphone listening: {}".format(exc))
        finally:
            with self._lifecycle_lock:
                self.running = False
                self._stopping = True
            self.on_status("Voice listener stopped.")

    def _remove_wake_word(self, transcript):
        match = re.match(r"^\s*(?:(?:hey|okay|ok)\s+)?" + re.escape(self.wake_word) + r"\b[\s,.:;!?-]*", transcript, re.I)
        if not match: return transcript, False
        return transcript[match.end():], True

    @staticmethod
    def _is_resume_phrase(command):
        phrase = re.sub(r"(?:(?:please|can you|could you|would you|will you)\s+)+", "", command.casefold(), count=1, flags=re.I)
        phrase = re.sub(r"\s+(?:please|now)[.!?]*$", "", phrase, flags=re.I)
        phrase = phrase.strip(" ,.!?")
        return phrase in {"start listening", "resume listening", "emergency stop", "cancel", "cancel current action"}

    def _handle(self, text, transcript=None):
        original = text if transcript is None else transcript
        self.last_transcript = original
        result = self.dispatcher.dispatch_transcript(text, original_transcript=original)
        self.last_command = self.dispatcher.last_intent
        if result["status"] in {"unsupported", "ambiguous", "execution_failed", "failed"}:
            logger.warning("DISPATCHER stage=%s intent=%s result=%s", result.get("stage"), result.get("intent"), result["status"])
        return result

    def status(self):
        return {"running": self.running, **{"listening_enabled": self.dispatcher.listening_enabled,
                "emergency_stopped": self.dispatcher.emergency_stopped,
                "pending_confirmation": self.dispatcher.pending is not None},
                "last_transcript": self.last_transcript}


if __name__ == "__main__":
    assistant = VoiceAssistant()
    try: assistant.start(background=False)
    except KeyboardInterrupt: assistant.stop()
