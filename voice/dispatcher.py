"""Safety validation, confirmation state, and single command dispatch."""
import logging
import time

from voice.catalog import Intent, normalized_command_text, parse_transcript

logger = logging.getLogger(__name__)

CONFIRM_REQUIRED = {"app.close", "window.close", "browser.tab.close_current", "text.delete_word", "text.delete_line", "text.replace", "system.power.sleep", "system.power.restart", "system.power.shutdown"}
SENSITIVE_PARAM_KEYS = {"text", "query", "url", "title", "old", "new", "subject", "expression"}
SENSITIVE_FEEDBACK_INTENTS = {"assistant.repeat", "accessibility.read_selection"}
VALID_RESULTS = {"executed", "action_requested", "failed", "unsupported", "ambiguous", "confirmation_required",
                 "cancelled", "ignored", "execution_failed"}
APP_TARGETS = {"chrome", "notepad", "calculator", "vscode", "file_explorer"}
REQUIRED_PARAMS = {
    "app.open": {"app": str}, "app.close": {"app": str}, "app.switch": {"app": str},
    "app.minimize": {"app": str}, "app.maximize": {"app": str}, "app.restore": {"app": str},
    "window.close": {"title": str}, "window.minimize": {"title": str},
    "window.maximize": {"title": str}, "window.restore": {"title": str}, "window.activate": {"title": str},
    "browser.navigate": {"url": str}, "browser.search": {"query": str},
    "keyboard.type_text": {"text": str}, "keyboard.shortcut": {"keys": list},
    "text.find": {"query": str}, "text.replace": {"old": str, "new": str},
    "assistant.calculate": {"expression": str}, "assistant.wikipedia": {"subject": str},
    "assistant.play": {"query": str}, "assistant.remember": {"text": str},
    "mouse.move": {"direction": str, "pixels": int}, "mouse.drag": {"start": tuple, "end": tuple},
    "system.brightness.set": {"percent": int},
}
KNOWN_INTENTS = set(REQUIRED_PARAMS) | set(CONFIRM_REQUIRED) | {
    "window.switch_next", "desktop.show", "browser.tab.new", "browser.tab.close_current",
    "browser.tab.next", "browser.tab.previous", "browser.refresh", "browser.back", "browser.forward",
    "mouse.click", "mouse.double_click", "mouse.right_click", "mouse.scroll_up", "mouse.scroll_down",
    "mouse.scroll_left", "mouse.scroll_right", "mouse.move_up", "mouse.move_down", "mouse.move_left",
    "mouse.move_right", "mouse.stop", "mouse.drag_start", "mouse.drag_drop", "keyboard.enter",
    "keyboard.escape", "keyboard.tab", "keyboard.space", "keyboard.backspace", "keyboard.delete",
    "keyboard.up", "keyboard.down", "keyboard.left", "keyboard.right", "keyboard.copy", "keyboard.paste",
    "keyboard.cut", "keyboard.undo", "keyboard.redo", "keyboard.select_all", "keyboard.save", "keyboard.home",
    "keyboard.end", "keyboard.page_up", "keyboard.page_down", "text.previous_word", "text.next_word",
    "text.previous_line", "text.next_line", "text.select_word", "text.select_line", "text.delete_word",
    "text.delete_line", "text.beginning", "text.end", "assistant.help", "assistant.repeat", "assistant.cancel",
    "assistant.greeting", "assistant.screenshot", "assistant.mouse_center", "assistant.quit", "assistant.clear_memory",
    "assistant.show_memory", "assistant.translate", "assistant.time", "assistant.date", "assistant.joke",
    "assistant.youtube", "voice.stop_listening", "voice.start_listening", "accessibility.read_selection",
    "system.volume_up", "system.volume_down", "system.mute", "system.unmute", "system.lock", "system.open_settings",
    "app.open_keyboard", "voice.start_eye", "voice.pause_eye", "voice.resume_eye", "voice.stop_eye",
    "voice.calibrate_gaze", "voice.stop_control", "system.emergency_stop",
    "system.power.sleep", "system.power.restart", "system.power.shutdown", "assistant.confirm", "assistant.reject",
    "system.brightness.up", "system.brightness.down", "system.brightness.set", "mouse.move", "mouse.drag",
}


def _safe_params(params):
    return {key: ("<redacted>" if key in SENSITIVE_PARAM_KEYS else value)
            for key, value in params.items()}


def _safe_transcript(transcript, intent):
    if intent is None or SENSITIVE_PARAM_KEYS.intersection(intent.params):
        return "<redacted; chars={}>".format(len(transcript))
    return transcript


class CommandDispatcher:
    def __init__(self, action_executor, feedback=None, cancel_executor=None, clock=time.monotonic):
        self.action_executor = action_executor
        self.feedback = feedback or (lambda _message: None)
        self.cancel_executor = cancel_executor or (lambda: None)
        self.clock = clock
        self.pending = None
        self.listening_enabled = True
        self.emergency_stopped = False
        self.last_response = ""
        self.last_intent = None
        self.last_stage = "idle"

    def cancel_pending(self):
        pending, self.pending = self.pending, None
        return pending

    def cancel_all(self):
        self.cancel_pending()
        self.cancel_executor()

    def expire_pending(self):
        if self.pending and self.clock() >= self.pending["expires_at"]:
            self.cancel_pending()
            self.last_stage = "confirmation"
            self._feedback("Confirmation expired; action cancelled.")
            logger.info("voice stage=confirmation result=expired")
            return True
        return False

    def dispatch_transcript(self, transcript, original_transcript=None):
        self.last_stage = "intent_detection"
        intent = parse_transcript(transcript)
        self.last_intent = intent
        original = transcript if original_transcript is None else original_transcript
        logger.info("ASR original_transcript=%r", _safe_transcript(original, intent))
        logger.info("WAKE transcript_after_removal=%r", _safe_transcript(transcript, intent))
        if intent is None:
            self.last_stage = "command_parsing"
            logger.info("PARSER result=unsupported")
            return self._result("unsupported", message="I did not recognize a supported command.")
        normalized = normalized_command_text(transcript)
        if SENSITIVE_PARAM_KEYS.intersection(intent.params):
            normalized = "{} <parameters redacted>".format(intent.name)
        logger.info("PARSER result=parsed normalized_command=%r intent=%s params=%r",
                    normalized, intent.name, _safe_params(intent.params))
        return self.dispatch(intent)

    def dispatch(self, intent):
        self.last_intent = intent
        self.last_stage = "dispatch"
        logger.info("DISPATCHER intent=%s route=received", intent.name)
        if self.emergency_stopped and intent.name != "assistant.cancel":
            return self._result("failed", intent, "Emergency stop is active.")
        if intent.name == "assistant.cancel":
            self.cancel_all()
            return self._result("cancelled", intent, "Pending or cancellable action stopped.")
        if intent.name == "assistant.confirm": return self._confirm(intent)
        if intent.name == "assistant.reject":
            self.cancel_all()
            return self._result("cancelled", intent, "Action cancelled.")
        if intent.name == "system.emergency_stop":
            self.emergency_stopped = True
            self.cancel_all()
            return self._execute(intent)
        if intent.name == "assistant.repeat":
            return self._result("executed", intent, self.last_response or "There is no previous response yet.")
        if intent.name == "voice.stop_listening":
            self.listening_enabled = False
            self.cancel_all()
            return self._result("executed", intent, "Listening paused. Say Chitti, start listening, or use the Start Voice Control button to resume.")
        if not self.listening_enabled and intent.name != "voice.start_listening":
            logger.info("DISPATCHER result=ignored intent=%s", intent.name)
            return {"status": "ignored", "intent": intent.name, "message": "Listening is paused."}
        if intent.name == "voice.start_listening":
            self.listening_enabled = True
            return self._result("executed", intent, "Listening resumed.")
        self.expire_pending()
        if self.pending:
            pending_name = self.pending["intent"].name
            self.cancel_pending()
            self._feedback("Pending {} action cancelled because a different command was received.".format(pending_name))
            logger.info("DISPATCHER pending_action=cancelled reason=unrelated_command")
        self.last_stage = "validation"
        try:
            validation = self._validate(intent)
        except Exception as exc:
            logger.warning("DISPATCHER validation=malformed intent=%s error_type=%s", intent.name, type(exc).__name__)
            return self._result("unsupported", intent, "Command parameters are malformed.")
        if validation:
            status, message = validation
            logger.warning("DISPATCHER validation=%s intent=%s", status, intent.name)
            return self._result(status, intent, message)
        logger.info("DISPATCHER validation=passed intent=%s", intent.name)
        if intent.name in CONFIRM_REQUIRED:
            self.last_stage = "confirmation"
            self.pending = {"intent": intent, "expires_at": self.clock() + 15}
            logger.info("DISPATCHER result=confirmation_required intent=%s timeout_seconds=15", intent.name)
            return self._result("confirmation_required", intent, "Confirm {} within 15 seconds, or say cancel.".format(intent.name))
        return self._execute(intent)

    def _confirm(self, command):
        self.last_stage = "confirmation"
        if not self.pending: return self._result("unsupported", command, "There is no pending action to confirm.")
        if self.clock() >= self.pending["expires_at"]:
            self.cancel_pending()
            return self._result("cancelled", command, "Confirmation expired; action cancelled.")
        intent = self.pending["intent"]
        self.pending = None
        return self._execute(intent)

    @staticmethod
    def _validate(intent):
        if not isinstance(getattr(intent, "name", None), str) or intent.name not in KNOWN_INTENTS:
            return "unsupported", "This command is not supported."
        if not isinstance(getattr(intent, "params", None), dict):
            return "unsupported", "Command parameters are malformed."
        required = REQUIRED_PARAMS.get(intent.name, {})
        for key, expected_type in required.items():
            value = intent.params.get(key)
            if not isinstance(value, expected_type) or (expected_type is str and not value.strip()):
                return "ambiguous", "Command is missing a valid {} parameter.".format(key)
        if intent.name.startswith("app.") and intent.name in {"app.open", "app.close", "app.switch", "app.minimize", "app.maximize", "app.restore"}:
            if intent.params["app"] not in APP_TARGETS:
                return "unsupported", "That application target is not supported."
        if intent.name.startswith("window.") and intent.name in {"window.close", "window.minimize", "window.maximize", "window.restore", "window.activate"}:
            if len(intent.params["title"].strip()) > 200:
                return "unsupported", "Window title is too long."
        app_targeted = {"app.open", "app.close", "app.switch", "app.minimize", "app.maximize", "app.restore"}
        window_targeted = {"window.close", "window.minimize", "window.maximize", "window.restore", "window.activate"}
        if intent.name in app_targeted and not intent.params.get("app"):
            return "ambiguous", "Name the application to control."
        if intent.name in window_targeted and not intent.params.get("title"):
            return "ambiguous", "Name the window to control."
        if intent.name == "browser.navigate":
            import urllib.parse
            url = intent.params["url"]
            explicit_scheme = urllib.parse.urlsplit(url).scheme
            if explicit_scheme and explicit_scheme.casefold() not in {"http", "https"}:
                return "unsupported", "Only valid http or https websites can be opened."
            if "://" not in url: url = "https://" + url
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc: return "unsupported", "Only valid http or https websites can be opened."
            intent.params["url"] = url
        if intent.name == "mouse.drag":
            for key, point in (("start", intent.params["start"]), ("end", intent.params["end"])):
                if len(point) != 2 or any(type(value) is not int or value < 0 or value > 99999 for value in point):
                    return "unsupported", "Drag coordinates must contain two valid non-negative integers."
        if intent.name == "mouse.move":
            if intent.params["direction"] not in {"up", "down", "left", "right"} or type(intent.params["pixels"]) is not int or not 1 <= intent.params["pixels"] <= 200:
                return "unsupported", "Mouse movement parameters are invalid."
        if intent.name == "keyboard.shortcut":
            keys = intent.params["keys"]
            allowed = {"ctrl", "alt", "shift", "win", "enter", "tab", "esc", "space", "backspace", "delete", "left", "right", "up", "down"}
            if not 2 <= len(keys) <= 4 or any(not isinstance(key, str) or key.casefold() not in allowed | set("abcdefghijklmnopqrstuvwxyz0123456789") | {"f" + str(n) for n in range(1, 13)} for key in keys):
                return "unsupported", "Keyboard shortcut contains unsupported keys."
        if intent.name == "system.brightness.set" and (type(intent.params["percent"]) is not int or not 0 <= intent.params["percent"] <= 100): return "unsupported", "Brightness must be from 0 to 100 percent."
        if intent.name == "assistant.calculate":
            expression = intent.params.get("expression", "")
            if not expression or len(expression) > 128 or any(c not in "0123456789+-*/().% " for c in expression): return "unsupported", "Only short basic arithmetic expressions are supported."
            try:
                import ast
                tree = ast.parse(expression, mode="eval")
                nodes = list(ast.walk(tree))
                if len(nodes) > 40:
                    return "unsupported", "Arithmetic expression is too complex."
                if any(isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
                       and abs(node.value) > 10**12 for node in nodes):
                    return "unsupported", "Arithmetic operand is too large."
                def depth(node, level=0):
                    if level > 12: return level
                    return max([level] + [depth(child, level + 1) for child in ast.iter_child_nodes(node)])
                if depth(tree) > 12:
                    return "unsupported", "Arithmetic expression is too deeply nested."
            except (SyntaxError, ValueError, RecursionError):
                return "unsupported", "Arithmetic expression is invalid."
        return None

    def _execute(self, intent):
        self.last_stage = "execution"
        action_name = getattr(self.action_executor, "__name__", type(self.action_executor).__name__)
        logger.info("DISPATCHER action_executor=%s intent=%s params=%r",
                    action_name, intent.name, _safe_params(intent.params))
        try:
            result = self.action_executor(intent)
        except Exception as exc:
            logger.error("RESULT stage=execution intent=%s status=execution_failed error_type=%s",
                         intent.name, type(exc).__name__)
            return self._result("execution_failed", intent, "Action failed during execution.")
        if not isinstance(result, dict):
            result = {"status": "executed" if result is True else "failed"}
        if result.get("status") not in VALID_RESULTS:
            logger.error("RESULT stage=execution intent=%s status=execution_failed reason=invalid_executor_status",
                         intent.name)
            return self._result("execution_failed", intent, "Action returned an invalid execution result.")
        result.setdefault("message", "Done." if result["status"] == "executed" else "The action did not complete.")
        if result["status"] in {"executed", "action_requested"}: self.last_response = result["message"]
        result.setdefault("intent", intent.name)
        result.setdefault("params", dict(intent.params))
        result.setdefault("stage", self.last_stage)
        self._feedback(result["message"])
        feedback_message = result["message"]
        if (SENSITIVE_PARAM_KEYS.intersection(intent.params)
                or intent.name in SENSITIVE_FEEDBACK_INTENTS
                or result["status"] == "execution_failed"):
            feedback_message = "<redacted>"
        logger.info("RESULT intent=%s status=%s feedback=%r",
                    intent.name, result["status"], feedback_message)
        return result

    def _feedback(self, message):
        try:
            self.feedback(message)
        except Exception as exc:
            logger.error("RESULT feedback=failed error_type=%s", type(exc).__name__)

    def _result(self, status, intent=None, message=""):
        result = {"status": status, "stage": self.last_stage}
        if intent: result.update(intent=intent.name, params=dict(intent.params))
        if message:
            result["message"] = message
            self._feedback(message)
            self.last_response = message
        safe_feedback = message
        if (intent and (SENSITIVE_PARAM_KEYS.intersection(intent.params)
                        or intent.name in SENSITIVE_FEEDBACK_INTENTS
                        or status == "execution_failed")):
            safe_feedback = "<redacted>"
        if intent:
            logger.info("RESULT intent=%s status=%s feedback=%r", intent.name, status, safe_feedback)
        else:
            logger.info("RESULT status=%s feedback=%r", status, safe_feedback)
        return result
