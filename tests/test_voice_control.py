"""Comprehensive mocked voice-command tests; no real desktop or OS action runs."""
import importlib
import logging
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch, call

from voice.catalog import APP_ALIASES, STATIC_PHRASES, Intent, command_catalog, normalized_command_text, parse_transcript
from voice.dispatcher import CommandDispatcher, CONFIRM_REQUIRED, KNOWN_INTENTS
from voice.gui_request import VoiceActionRequest


def load_voice_and_adapter():
    sr = types.ModuleType("speech_recognition")
    sr.Recognizer = Mock
    sr.Microphone = Mock
    for name in ("WaitTimeoutError", "UnknownValueError", "RequestError"):
        setattr(sr, name, type(name, (Exception,), {}))
    tts = types.ModuleType("pyttsx3")
    tts.init = Mock(side_effect=RuntimeError("mock TTS disabled"))
    gui = types.ModuleType("pyautogui")
    methods = ("hotkey", "press", "click", "doubleClick", "scroll", "hscroll", "write",
               "moveRel", "moveTo", "mouseDown", "mouseUp", "position", "size", "screenshot")
    for name in methods: setattr(gui, name, Mock())
    gui.position.return_value = (10, 10)
    gui.size.return_value = (1920, 1080)
    wikipedia = types.ModuleType("wikipedia")
    wikipedia.exceptions = types.SimpleNamespace(
        DisambiguationError=type("DisambiguationError", (Exception,), {}),
        PageError=type("PageError", (Exception,), {}),
    )
    wikipedia.summary = Mock(return_value="A mocked summary.")
    with patch.dict(sys.modules, {"speech_recognition": sr, "pyttsx3": tts,
                                  "pyautogui": gui, "wikipedia": wikipedia}):
        for name in ("voice.assistant", "voice.windows_adapter"):
            sys.modules.pop(name, None)
        assistant_module = importlib.import_module("voice.assistant")
        adapter_module = importlib.import_module("voice.windows_adapter")
        adapter_module.pyautogui = gui
    return assistant_module, adapter_module, gui


assistant_module, adapter_module, gui = load_voice_and_adapter()


class FakeClock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now


class FakeWindows:
    def __init__(self, windows, foreground=None, activate_ok=True):
        self.items = list(windows)
        self.active = foreground
        self.activate_ok = activate_ok
        self.operations = []
    def windows(self): return list(self.items)
    def foreground(self): return self.active
    def activate(self, window):
        if not self.activate_ok: return False
        self.active = window
        return True
    def operate(self, window, operation):
        self.operations.append((window, operation)); return True
    def screen_size(self): return (1920, 1080)
    def lock(self): return True


def mock_adapter(windows=None, foreground=None, **kwargs):
    api = FakeWindows(windows or [], foreground=foreground)
    desktop = types.SimpleNamespace(**{name: Mock() for name in (
        "hotkey", "press", "click", "doubleClick", "scroll", "hscroll", "write",
        "moveRel", "moveTo", "mouseDown", "mouseUp", "position", "size", "screenshot")})
    desktop.position.return_value = (10, 10)
    desktop.size.return_value = (1920, 1080)
    audio_state = {"volume": 24000, "muted": False}
    def set_volume(value): audio_state["volume"] = value
    def set_mute(value): audio_state["muted"] = bool(value)
    audio = types.SimpleNamespace(
        get_volume=Mock(side_effect=lambda: audio_state["volume"]),
        set_volume=Mock(side_effect=set_volume),
        get_mute=Mock(side_effect=lambda: audio_state["muted"]),
        set_mute=Mock(side_effect=set_mute),
        state=audio_state,
    )
    adapter = adapter_module.WindowsControlAdapter(api=api, desktop=desktop, opener=Mock(),
                                                    shell_run=Mock(return_value=types.SimpleNamespace(returncode=0, stdout="50")),
                                                    audio=audio, **kwargs)
    return adapter, api, desktop, audio


class CatalogTests(unittest.TestCase):
    def test_escape_and_volume_alias_groups_resolve_without_conflicts(self):
        groups = {
            "keyboard.escape": ("press escape", "press esc", "escape"),
            "system.volume_up": ("increase volume", "turn volume up", "volume up", "raise volume",
                                 "increase sound", "turn sound up", "sound up", "raise sound"),
            "system.volume_down": ("decrease volume", "turn volume down", "volume down", "lower volume",
                                   "decrease sound", "turn sound down", "sound down", "lower sound"),
            "system.mute": ("mute", "mute volume", "mute audio", "silent", "turn on silent",
                            "sound of", "sound off", "turn sound off"),
            "system.unmute": ("unmute", "unmute volume", "unmute audio", "turn off silent",
                              "turn of silent", "sound on", "turn sound on"),
        }
        for expected, phrases in groups.items():
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    parsed = parse_transcript(phrase)
                    self.assertIsNotNone(parsed)
                    self.assertEqual(parsed.name, expected)

    def test_escape_and_volume_aliases_accept_supported_transcript_variations(self):
        cases = {
            "PRESS ESCAPE!": "keyboard.escape",
            "Hey Chitti, please turn sound off!": "system.mute",
            "Chitti, could you lower volume now?": "system.volume_down",
            "Chitti, raise sound please.": "system.volume_up",
            "turn of silent": "system.unmute",  # legacy alias remains supported
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                parsed = parse_transcript(phrase)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.name, expected)

    def test_click_phrase_resolves_to_mouse_without_changing_enter_aliases(self):
        for phrase, expected in (
                ("click", "mouse.click"),
                ("left click", "mouse.click"),
                ("press enter", "keyboard.enter"),
                ("hit enter", "keyboard.enter"),
                ("enter", "keyboard.enter"),
                ("right click", "mouse.right_click"),
                ("double click", "mouse.double_click"),
                ("press escape", "keyboard.escape"),
                ("press tab", "keyboard.tab")):
            with self.subTest(phrase=phrase):
                parsed = parse_transcript(phrase)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.name, expected)

    def test_every_static_alias_resolves_to_its_catalog_intent(self):
        self.assertTrue(set(STATIC_PHRASES).issubset(KNOWN_INTENTS))
        for intent_name, phrases in STATIC_PHRASES.items():
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    parsed = parse_transcript(phrase)
                    self.assertIsNotNone(parsed)
                    self.assertEqual(parsed.name, intent_name)

    def test_parameterized_catalog_patterns_and_application_targets(self):
        for app, aliases in APP_ALIASES.items():
            for alias in aliases:
                for verb, expected in (("open", "app.open"), ("launch", "app.open"),
                                       ("start", "app.open"), ("close app", "app.close"),
                                       ("close application", "app.close"), ("switch to", "app.switch"),
                                       ("maximize", "app.maximize"), ("minimize", "app.minimize"),
                                       ("restore", "app.restore")):
                    parsed = parse_transcript("{} {}".format(verb, alias))
                    self.assertEqual(parsed.name, expected)
                    self.assertEqual(parsed.params["app"], app)
                self.assertEqual(parse_transcript("open the {} app".format(alias)).params["app"], app)
                self.assertEqual(parse_transcript("maximize the {} application".format(alias)).params["app"], app)
        for phrase, name, key in (
            ("close window named Editor", "window.close", "title"),
            ("minimize window named Editor", "window.minimize", "title"),
            ("maximize window named Editor", "window.maximize", "title"),
            ("restore window named Editor", "window.restore", "title"),
            ("switch to window named Editor", "window.activate", "title"),
            ("search for blue birds", "browser.search", "query"),
            ("search Google for blue birds", "browser.search", "query"),
            ("find a phrase", "text.find", "query"),
            ("calculate 12*(3+1)", "assistant.calculate", "expression"),
            ("set brightness to 60 percent", "system.brightness.set", "percent"),
        ):
            parsed = parse_transcript(phrase)
            self.assertEqual(parsed.name, name)
            self.assertIn(key, parsed.params)

    def test_parameterized_intents_dispatch_through_the_single_mocked_path(self):
        cases = [
            ("open Notepad", "app.open"), ("close app Notepad", "app.close"),
            ("switch to Chrome", "app.switch"), ("maximize Calculator", "app.maximize"),
            ("minimize VS Code", "app.minimize"), ("restore File Explorer", "app.restore"),
            ("close window named Editor", "window.close"),
            ("switch to window named Editor", "window.activate"),
            ("search Google for birds", "browser.search"),
            ("go to https://example.com", "browser.navigate"),
            ("type Hello!", "keyboard.type_text"), ("find needle", "text.find"),
            ("replace old with new", "text.replace"), ("calculate 2+2", "assistant.calculate"),
            ("who is Ada Lovelace", "assistant.wikipedia"), ("play jazz", "assistant.play"),
            ("remember that I like tea", "assistant.remember"),
            ("set brightness to 60 percent", "system.brightness.set"),
            ("move cursor left 30 pixels", "mouse.move"),
            ("drag from 1 2 to 30 40", "mouse.drag"),
        ]
        for transcript, expected in cases:
            with self.subTest(transcript=transcript):
                executor = Mock(return_value={"status": "executed"})
                dispatcher = CommandDispatcher(executor)
                result = dispatcher.dispatch_transcript(transcript)
                if result["status"] == "confirmation_required":
                    result = dispatcher.dispatch_transcript("confirm")
                self.assertEqual(result["intent"], expected)
                self.assertEqual(result["status"], "executed")
                executor.assert_called_once()

    def test_realistic_transcript_wrappers_and_terminal_punctuation(self):
        cases = {
            "Hey Chitti, scroll down.": "mouse.scroll_down",
            "please scroll down": "mouse.scroll_down",
            "could you open calculator?": "app.open",
            "minimize Notepad.": "app.minimize",
            "go to https://example.com.": "browser.navigate",
            "set brightness to 60 percent.": "system.brightness.set",
            "close window named Editor.": "window.close",
            "search for blue birds.": "browser.search",
            "what’s the time?": "assistant.time",
        }
        for transcript, expected in cases.items():
            with self.subTest(transcript=transcript):
                self.assertEqual(parse_transcript(transcript).name, expected)
        self.assertEqual(parse_transcript("close window named Editor.").params["title"], "Editor")
        self.assertEqual(parse_transcript("close   window   named   Draft Editor").params["title"], "Draft Editor")
        self.assertIsNone(parse_transcript("please close windows"))
        self.assertEqual(normalized_command_text("Hey Chitti, please scroll down."), "scroll down")
        self.assertEqual(normalized_command_text("type private dictated words"), "type <verbatim payload>")

    def test_all_static_aliases_dispatch_only_to_mock_executor_or_safe_guard(self):
        for intent_name, phrases in STATIC_PHRASES.items():
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    executor = Mock(return_value={"status": "executed"})
                    result = CommandDispatcher(executor).dispatch_transcript(phrase)
                    self.assertIn(result["status"], {"executed", "action_requested", "confirmation_required", "ambiguous", "cancelled", "unsupported"})
                    if result["status"] != "executed": executor.assert_not_called()

    def test_application_target_aliases_and_parameter_commands(self):
        for app, aliases in APP_ALIASES.items():
            for alias in aliases:
                self.assertEqual(parse_transcript("open " + alias).params["app"], app)
                self.assertEqual(parse_transcript("close " + alias).name, "app.close")
                self.assertEqual(parse_transcript("switch to " + alias).params["app"], app)
                self.assertEqual(parse_transcript("maximize " + alias).params["app"], app)
        self.assertEqual(parse_transcript("close window named Draft - Editor").params["title"], "Draft - Editor")
        self.assertEqual(parse_transcript("Search Google for blue birds").params["query"], "blue birds")
        self.assertEqual(parse_transcript("go to https://example.org/path").name, "browser.navigate")
        self.assertEqual(parse_transcript("replace old words with New words").params,
                         {"old": "old words", "new": "New words"})
        self.assertEqual(parse_transcript("move cursor left 900 pixels").params["pixels"], 200)
        self.assertEqual(parse_transcript("drag from 1 2 to 500 400").params["end"], (500, 400))

    def test_dictation_payload_preserves_punctuation_case_and_repeated_spacing(self):
        parsed = parse_transcript("type  Hello,  world!  Don’t normalize this.  ")
        self.assertEqual(parsed.name, "keyboard.type_text")
        self.assertEqual(parsed.params["text"], " Hello,  world!  Don’t normalize this.  ")
        self.assertEqual(parse_transcript("type please").params["text"], "please")
        self.assertEqual(parse_transcript("type  I said now please ").params["text"], " I said now please ")

    def test_ambiguous_shortcuts_and_unsupported_text_do_not_get_fuzzy_mapped(self):
        for phrase in ("maximize", "minimize", "restore", "close app", "close window"):
            parsed = parse_transcript(phrase)
            self.assertIsNotNone(parsed)
            self.assertFalse(parsed.params)
        self.assertIsNone(parse_transcript("maximizer"))
        self.assertIsNone(parse_transcript("close tabs"))
        self.assertIsNone(parse_transcript("turn off my laptop now"))

    def test_public_catalog_is_available_for_help(self):
        catalog = command_catalog()
        self.assertIn("browser.tab.new", catalog)
        self.assertIn("system.power.shutdown", catalog)
        self.assertIn("app.target.vscode", catalog)


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.executor = Mock(return_value={"status": "executed", "message": "Mock action completed."})
        self.feedback = Mock()
        self.cancel = Mock()
        self.dispatcher = CommandDispatcher(self.executor, self.feedback, self.cancel, self.clock)

    def test_ambiguous_unsupported_and_failed_are_distinct_and_no_fallback_occurs(self):
        self.assertEqual(self.dispatcher.dispatch_transcript("maximize")["status"], "ambiguous")
        self.assertEqual(self.dispatcher.dispatch_transcript("close tabs")["status"], "unsupported")
        self.executor.side_effect = RuntimeError("mock error")
        result = self.dispatcher.dispatch_transcript("copy")
        self.assertEqual(result["status"], "execution_failed")
        self.executor.assert_called_once()

    def test_all_confirm_required_intents_wait_for_explicit_confirmation(self):
        cases = [Intent("app.close", {"app": "notepad"}), Intent("window.close", {"title": "Editor"}),
                 Intent("browser.tab.close_current"), Intent("text.delete_word"), Intent("text.delete_line"),
                 Intent("text.replace", {"old": "x", "new": "y"}), Intent("system.power.sleep"),
                 Intent("system.power.restart"), Intent("system.power.shutdown")]
        for intent in cases:
            with self.subTest(intent=intent.name):
                self.dispatcher.pending = None
                self.executor.reset_mock()
                result = self.dispatcher.dispatch(intent)
                self.assertEqual(result["status"], "confirmation_required")
                self.assertEqual(self.executor.call_count, 0)
                self.assertEqual(self.dispatcher.dispatch_transcript("confirm")["status"], "executed")
                self.executor.assert_called_once_with(intent)

    def test_confirmation_cancel_and_timeout_never_execute(self):
        dangerous = Intent("system.power.shutdown")
        self.dispatcher.dispatch(dangerous)
        self.assertEqual(self.dispatcher.dispatch_transcript("cancel")["status"], "cancelled")
        self.executor.assert_not_called()
        self.assertEqual(self.cancel.call_count, 1)
        self.dispatcher.dispatch(dangerous)
        self.clock.now += 16
        result = self.dispatcher.dispatch_transcript("confirm")
        self.assertEqual(result["status"], "cancelled")
        self.executor.assert_not_called()

    def test_expired_confirmation_is_cleared_without_another_transcript(self):
        self.dispatcher.dispatch(Intent("system.power.shutdown"))
        self.clock.now += 16
        self.assertTrue(self.dispatcher.expire_pending())
        self.assertIsNone(self.dispatcher.pending)
        self.assertEqual(self.feedback.call_args.args[0], "Confirmation expired; action cancelled.")
        self.executor.assert_not_called()

    def test_emergency_stop_cancels_pending_and_blocks_later_dispatch(self):
        self.dispatcher.dispatch(Intent("system.power.restart"))
        result = self.dispatcher.dispatch_transcript("emergency stop")
        self.assertEqual(result["status"], "executed")
        self.assertIsNone(self.dispatcher.pending)
        self.cancel.assert_called_once()
        self.assertEqual(self.dispatcher.dispatch_transcript("copy")["status"], "failed")

    def test_pending_confirmation_expires_and_unrelated_safe_command_can_run(self):
        self.dispatcher.dispatch(Intent("system.power.shutdown"))
        result = self.dispatcher.dispatch_transcript("copy")
        self.assertEqual(result["status"], "executed")
        self.assertEqual(self.executor.call_args.args[0].name, "keyboard.copy")
        self.assertIsNone(self.dispatcher.pending)

    def test_reject_cancels_pending_destructive_action(self):
        self.dispatcher.dispatch(Intent("system.power.restart"))
        result = self.dispatcher.dispatch_transcript("no")
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(self.dispatcher.pending)
        self.executor.assert_not_called()

    def test_feedback_failure_does_not_turn_successful_action_into_failure(self):
        self.dispatcher.feedback.side_effect = RuntimeError("mock feedback failure")
        result = self.dispatcher.dispatch_transcript("copy")
        self.assertEqual(result["status"], "executed")
        self.executor.assert_called_once()

    def test_invalid_executor_status_is_structured_failure(self):
        self.executor.return_value = {"status": "maybe"}
        self.assertEqual(self.dispatcher.dispatch_transcript("copy")["status"], "execution_failed")

    def test_malformed_direct_intents_fail_validation_without_executor(self):
        cases = [Intent("mouse.drag", {"start": (1, 2)}),
                 Intent("system.brightness.set", {}),
                 Intent("system.brightness.set", {"percent": "50"}),
                 Intent("browser.navigate", {"url": 42}),
                 Intent("keyboard.type_text", {"text": 42}),
                 Intent("mouse.drag", {"start": (1, 2), "end": (3,)})]
        for intent in cases:
            with self.subTest(intent=intent):
                self.assertIn(self.dispatcher.dispatch(intent)["status"], {"ambiguous", "unsupported"})
        self.executor.assert_not_called()

    def test_calculator_size_and_depth_are_bounded(self):
        for expression in ("1+" * 70 + "1", "-" * 20 + "1"):
            with self.subTest(expression=expression[:20]):
                self.assertEqual(self.dispatcher.dispatch_transcript("calculate " + expression)["status"], "unsupported")
        self.executor.assert_not_called()

    def test_listener_pause_wake_resume_and_cancellation(self):
        self.dispatcher.dispatch_transcript("stop listening")
        self.assertFalse(self.dispatcher.listening_enabled)
        self.cancel.assert_called_once()
        self.assertEqual(self.dispatcher.dispatch_transcript("copy")["status"], "ignored")
        self.assertEqual(self.dispatcher.dispatch_transcript("start listening")["status"], "executed")
        self.assertTrue(self.dispatcher.listening_enabled)

    def test_broad_namespace_validation_no_longer_rejects_non_target_actions(self):
        self.assertEqual(self.dispatcher.dispatch_transcript("switch window")["status"], "executed")
        self.executor.reset_mock()
        self.assertEqual(self.dispatcher.dispatch_transcript("open keyboard")["status"], "executed")
        self.assertEqual(self.executor.call_count, 1)

    def test_diagnostics_include_pipeline_stages_and_redact_dictated_payload(self):
        for transcript in ("type private-token-123", "search for confidential health query"):
            with self.subTest(transcript=transcript), self.assertLogs("voice.dispatcher", level="INFO") as captured:
                result = self.dispatcher.dispatch_transcript(transcript, original_transcript="Hey Chitti, " + transcript)
            output = "\n".join(captured.output)
            self.assertEqual(result["status"], "executed")
            for stage in ("ASR", "WAKE", "PARSER", "DISPATCHER", "RESULT"):
                self.assertIn(stage, output)
            self.assertNotIn("private-token-123", output)
            self.assertNotIn("confidential health query", output)
            self.assertIn("<redacted", output)

    def test_target_and_url_validation(self):
        self.assertEqual(self.dispatcher.dispatch_transcript("close Notepad")["status"], "confirmation_required")
        self.dispatcher.dispatch_transcript("cancel")
        self.assertEqual(self.dispatcher.dispatch_transcript("go to javascript:alert(1)")["status"], "unsupported")
        self.assertEqual(self.dispatcher.dispatch_transcript("calculate 1+bad")["status"], "unsupported")
        self.executor.assert_not_called()


class VoicePipelineTests(unittest.TestCase):
    def test_timed_out_or_cancelled_gui_request_cannot_be_claimed_later(self):
        request = VoiceActionRequest(Intent("keyboard.copy"))
        self.assertTrue(request.wait(0))
        self.assertFalse(request.claim())
        self.assertFalse(request.complete(value={"status": "executed"}))
        self.assertEqual(request.result["value"]["status"], "cancelled")
        cancelled = VoiceActionRequest(Intent("keyboard.paste"))
        self.assertTrue(cancelled.cancel_if_pending())
        self.assertFalse(cancelled.claim())
        self.assertFalse(cancelled.wait(0))
        self.assertEqual(cancelled.result["value"]["status"], "cancelled")

    def test_claimed_gui_request_returns_completed_result_once(self):
        request = VoiceActionRequest(Intent("keyboard.copy"))
        self.assertTrue(request.claim())
        self.assertFalse(request.claim())
        self.assertTrue(request.complete(value={"status": "executed"}))
        self.assertFalse(request.complete(value={"status": "executed"}))
        self.assertEqual(request.result["value"]["status"], "executed")

    def test_tts_queue_replaces_stale_pending_feedback(self):
        import queue
        instance = assistant_module.TTS.__new__(assistant_module.TTS)
        instance.speech_queue = queue.Queue(maxsize=1)
        instance.engine_error = None
        instance.speak("old status")
        instance.speak("current result")
        self.assertEqual(instance.speech_queue.get_nowait(), "current result")

    def test_diagnostics_file_is_created_at_local_app_data_path(self):
        logger_names = ("voice", "vision.camera_pipeline", "vision.eye_control",
                        "desktop_input", "vkeyboard")
        old_states = {name: (logging.getLogger(name).level, logging.getLogger(name).propagate)
                      for name in logger_names}
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"LOCALAPPDATA": directory}):
                log_path = assistant_module.configure_voice_diagnostics()
            voice_logger = logging.getLogger("voice")
            handler = next(h for h in voice_logger.handlers if getattr(h, "_synapse_voice_diagnostics", False))
            logging.getLogger("voice.test").info("RESULT test=diagnostics_file")
            logging.getLogger("vision.camera_pipeline").info("CAMERA test=diagnostics_file")
            logging.getLogger("vision.eye_control").info("BLINK event=eye_closed side=left")
            logging.getLogger("vkeyboard").info("VKEYBOARD key_handler_called key=A")
            handler.flush()
            with open(log_path, encoding="utf-8") as log_file:
                content = log_file.read()
            self.assertIn("RESULT test=diagnostics_file", content)
            self.assertIn("CAMERA test=diagnostics_file", content)
            self.assertIn("BLINK event=eye_closed", content)
            self.assertIn("VKEYBOARD key_handler_called", content)
            for name in logger_names:
                logging.getLogger(name).removeHandler(handler)
            handler.close()
        for name, (level, propagate) in old_states.items():
            logging.getLogger(name).setLevel(level)
            logging.getLogger(name).propagate = propagate

    def test_handle_keeps_raw_transcript_separate_from_parsed_command(self):
        recognizer = types.SimpleNamespace(energy_threshold=0, dynamic_energy_threshold=False,
            dynamic_energy_adjustment_damping=0, dynamic_energy_ratio=0, pause_threshold=0,
            phrase_threshold=0, non_speaking_duration=0)
        actions = Mock(return_value={"status": "executed", "message": "Typed."})
        assistant = assistant_module.VoiceAssistant(
            recognizer=recognizer, microphone=object(), action_executor=actions,
            speech_output=Mock())
        result = assistant._handle("type  Hello!", transcript="Chitti, type  Hello!")
        self.assertEqual(result["status"], "executed")
        self.assertEqual(assistant.last_transcript, "Chitti, type  Hello!")
        self.assertEqual(assistant.last_command.params["text"], " Hello!")
        actions.assert_called_once()

    def test_wake_word_removal_is_prefix_only(self):
        recognizer = types.SimpleNamespace(energy_threshold=0, dynamic_energy_threshold=False,
            dynamic_energy_adjustment_damping=0, dynamic_energy_ratio=0, pause_threshold=0,
            phrase_threshold=0, non_speaking_duration=0)
        assistant = assistant_module.VoiceAssistant(recognizer=recognizer, microphone=object(),
                                                     speech_output=Mock())
        self.assertEqual(assistant._remove_wake_word("Chitti, copy"), ("copy", True))
        self.assertEqual(assistant._remove_wake_word("Hey Chitti, copy"), ("copy", True))
        self.assertEqual(assistant._remove_wake_word("Okay Chitti! copy"), ("copy", True))
        self.assertEqual(assistant._remove_wake_word("copy Chitti"), ("copy Chitti", False))
        self.assertTrue(assistant._is_resume_phrase("could you please start listening?"))
        self.assertTrue(assistant._is_resume_phrase("resume listening please"))
        self.assertFalse(assistant._is_resume_phrase("close app Notepad"))

    def test_mock_microphone_and_google_transcript_reach_the_dispatcher(self):
        assistant_ref = {}
        class Microphone:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
        class Recognizer:
            energy_threshold = 0
            dynamic_energy_threshold = False
            dynamic_energy_adjustment_damping = 0
            dynamic_energy_ratio = 0
            pause_threshold = 0
            phrase_threshold = 0
            non_speaking_duration = 0
            def adjust_for_ambient_noise(self, *_args, **_kwargs): pass
            def listen(self, *_args, **_kwargs): return object()
            def recognize_google(self, _audio):
                assistant_ref["assistant"].running = False
                return "Hey Chitti, please scroll down."
        executor = Mock(return_value={"status": "executed", "message": "Scrolled."})
        assistant = assistant_module.VoiceAssistant(recognizer=Recognizer(), microphone=Microphone(),
                                                     action_executor=executor, speech_output=Mock())
        assistant_ref["assistant"] = assistant
        assistant.running = True
        assistant._listen_loop()
        self.assertEqual(assistant.last_transcript, "Hey Chitti, please scroll down.")
        self.assertEqual(assistant.last_command.name, "mouse.scroll_down")
        executor.assert_called_once()

    def test_microphone_capture_failure_is_classified_and_stops_cleanly(self):
        statuses = []
        class Microphone:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
        class Recognizer:
            energy_threshold = 0
            dynamic_energy_threshold = False
            dynamic_energy_adjustment_damping = 0
            dynamic_energy_ratio = 0
            pause_threshold = 0
            phrase_threshold = 0
            non_speaking_duration = 0
            def adjust_for_ambient_noise(self, *_args, **_kwargs): pass
            def listen(self, *_args, **_kwargs): raise OSError("mock device disconnected")
            def recognize_google(self, _audio): raise AssertionError("ASR must not be called")
        assistant = assistant_module.VoiceAssistant(recognizer=Recognizer(), microphone=Microphone(),
            on_status=statuses.append, speech_output=Mock())
        assistant.running = True
        with self.assertLogs("voice.assistant", level="ERROR") as captured:
            assistant._listen_loop()
        self.assertIn("MICROPHONE", "\n".join(captured.output))
        self.assertFalse(assistant.running)
        self.assertTrue(any("restart voice control" in message for message in statuses))

    def test_google_request_error_reports_transport_reason_without_transcript(self):
        statuses = []
        assistant_ref = {}
        class Microphone:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
        class Recognizer:
            energy_threshold = 0
            dynamic_energy_threshold = False
            dynamic_energy_adjustment_damping = 0
            dynamic_energy_ratio = 0
            pause_threshold = 0
            phrase_threshold = 0
            non_speaking_duration = 0
            def adjust_for_ambient_noise(self, *_args, **_kwargs): pass
            def listen(self, *_args, **_kwargs): return object()
            def recognize_google(self, _audio):
                assistant_ref["assistant"].running = False
                raise assistant_module.sr.RequestError(
                    "recognition connection failed: timed out for https://example.invalid/?key=secret"
                )
        assistant = assistant_module.VoiceAssistant(
            recognizer=Recognizer(), microphone=Microphone(), on_status=statuses.append,
            speech_output=Mock())
        assistant_ref["assistant"] = assistant
        assistant.running = True
        with patch.object(assistant_module.time, "sleep"), \
             self.assertLogs("voice.assistant", level="ERROR") as logs:
            assistant._listen_loop()
        output = " ".join(logs.output)
        self.assertIn("category=network_connection", output)
        self.assertIn("<url>", output)
        self.assertNotIn("key=secret", output)
        self.assertTrue(any("Could not connect to Google speech recognition" in item for item in statuses))
        self.assertFalse(any("private dictated" in item for item in statuses))

    def test_start_does_not_revive_a_worker_that_is_still_stopping(self):
        class RunningWorker:
            def is_alive(self): return True
        recognizer = types.SimpleNamespace(energy_threshold=0, dynamic_energy_threshold=False,
            dynamic_energy_adjustment_damping=0, dynamic_energy_ratio=0, pause_threshold=0,
            phrase_threshold=0, non_speaking_duration=0)
        statuses = []
        assistant = assistant_module.VoiceAssistant(recognizer=recognizer, microphone=object(),
            on_status=statuses.append, speech_output=Mock())
        assistant.worker = RunningWorker()
        assistant.running = False
        with patch.object(assistant_module, "configure_voice_diagnostics", return_value=None), \
             patch.object(assistant, "_listen_loop") as listener:
            assistant.start(background=True)
        listener.assert_not_called()
        self.assertFalse(assistant.running)
        self.assertTrue(any("still stopping" in message for message in statuses))

    def test_static_catalog_transcripts_route_through_voice_pipeline_once(self):
        for _intent_name, phrases in STATIC_PHRASES.items():
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    recognizer = types.SimpleNamespace(energy_threshold=0, dynamic_energy_threshold=False,
                        dynamic_energy_adjustment_damping=0, dynamic_energy_ratio=0, pause_threshold=0,
                        phrase_threshold=0, non_speaking_duration=0)
                    executor = Mock(return_value={"status": "executed", "message": "mocked"})
                    assistant = assistant_module.VoiceAssistant(recognizer=recognizer,
                        microphone=object(), action_executor=executor, speech_output=Mock())
                    result = assistant._handle(phrase, transcript=phrase)
                    self.assertIsNotNone(assistant.last_command)
                    self.assertEqual(assistant.last_command.name, _intent_name)
                    if _intent_name in {"window.switch_next", "app.open_keyboard"}:
                        self.assertEqual(result["status"], "executed")


class AdapterTests(unittest.TestCase):
    def setUp(self):
        for name in ("hotkey", "press", "click", "doubleClick", "scroll", "hscroll", "write", "moveRel", "moveTo", "mouseDown", "mouseUp"):
            getattr(gui, name).reset_mock()

    def test_browser_shortcuts_focus_and_verify_browser_before_keys(self):
        chrome = {"hwnd": 2, "pid": 20, "title": "Chrome", "exe": "chrome.exe"}
        other = {"hwnd": 3, "pid": 30, "title": "Notepad", "exe": "notepad.exe"}
        adapter, api, desktop, _ = mock_adapter([chrome, other], foreground=other)
        result = adapter.execute(Intent("browser.refresh"))
        self.assertEqual(result["status"], "action_requested")
        self.assertEqual(api.active, chrome)
        desktop.hotkey.assert_called_once_with("ctrl", "r")
        adapter2, api2, desktop2, _ = mock_adapter([chrome, other], foreground=other)
        api2.activate_ok = False
        result = adapter2.execute(Intent("browser.refresh"))
        self.assertEqual(result["status"], "failed")
        desktop2.hotkey.assert_not_called()
        own_dashboard = {"hwnd": 1, "pid": 9, "title": "SynapseOS dashboard", "exe": "python.exe"}
        branded_page = {"hwnd": 4, "pid": 20, "title": "SynapseOS documentation", "exe": "chrome.exe"}
        adapter3, _api3, desktop3, _ = mock_adapter([own_dashboard, branded_page],
                                                     foreground=branded_page, own_pid=9)
        self.assertEqual(adapter3.execute(Intent("browser.refresh"))["status"], "action_requested")
        desktop3.hotkey.assert_called_once_with("ctrl", "r")

    def test_virtual_keyboard_input_requires_shared_transaction(self):
        from desktop_input import DesktopInputCoordinator
        DesktopInputBlocked = adapter_module.DesktopInputBlocked
        coordinator = DesktopInputCoordinator()
        adapter, _api, desktop, _ = mock_adapter(input_coordinator=coordinator)
        with self.assertRaises(DesktopInputBlocked):
            adapter.keyboard_input_in_transaction("write", "a")
        adapter.input_coordinator.run_voice_transaction(
            lambda: adapter.keyboard_input_in_transaction("write", "a")
        )
        desktop.write.assert_called_once_with("a")

    def test_named_targets_exclude_synapse_dashboard_and_disambiguate_windows(self):
        own = {"hwnd": 1, "pid": 9, "title": "SynapseOS dashboard", "exe": "python.exe"}
        note = {"hwnd": 2, "pid": 10, "title": "Notes - Notepad", "exe": "notepad.exe"}
        adapter, api, _, _ = mock_adapter([own, note], own_pid=9)
        result = adapter.execute(Intent("app.maximize", {"app": "notepad"}))
        self.assertEqual(result["status"], "action_requested")
        self.assertEqual(api.operations, [(note, "maximize")])
        matches = adapter._windows()
        self.assertEqual(matches, [note])
        switch, switch_api, desktop, _ = mock_adapter([own, note], foreground=own)
        self.assertEqual(switch.execute(Intent("window.switch_next"))["status"], "executed")
        self.assertEqual(switch_api.active, note)
        desktop.hotkey.assert_not_called()
        ambiguous = {"hwnd": 3, "pid": 11, "title": "Notes - Copy", "exe": "notepad.exe"}
        adapter.api.items.append(ambiguous)
        result = adapter.execute(Intent("window.close", {"title": "Notes"}))
        self.assertEqual(result["status"], "ambiguous")

    def test_calculator_window_matching_supports_application_frame_host(self):
        calculator = {"hwnd": 4, "pid": 41, "title": "Calculator", "exe": "ApplicationFrameHost.exe"}
        adapter, api, _, _ = mock_adapter([calculator], foreground=calculator)
        self.assertEqual(adapter._app_windows("calculator"), [calculator])
        result = adapter.execute(Intent("app.maximize", {"app": "calculator"}))
        self.assertEqual(result["status"], "action_requested")
        self.assertEqual(api.operations, [(calculator, "maximize")])

    def test_close_app_refuses_multiple_matching_windows(self):
        first = {"hwnd": 2, "pid": 10, "title": "One", "exe": "notepad.exe"}
        second = {"hwnd": 3, "pid": 11, "title": "Two", "exe": "notepad.exe"}
        adapter, api, _, _ = mock_adapter([first, second])
        result = adapter.execute(Intent("app.close", {"app": "notepad"}))
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(api.operations, [])
        dispatcher = CommandDispatcher(adapter.execute)
        self.assertEqual(dispatcher.dispatch_transcript("close app Notepad")["status"], "confirmation_required")
        self.assertEqual(dispatcher.dispatch_transcript("confirm")["status"], "ambiguous")
        self.assertEqual(api.operations, [])

    def test_drag_failure_always_releases_mouse_button(self):
        adapter, _, desktop, _ = mock_adapter()
        desktop.moveTo.side_effect = [None, RuntimeError("mock move failed")]
        with self.assertRaises(RuntimeError):
            adapter.execute(Intent("mouse.drag", {"start": (1, 2), "end": (10, 20)}))
        desktop.mouseUp.assert_called_once()
        self.assertFalse(adapter.dragging)

    def test_app_launch_uses_mocked_process_launcher(self):
        adapter, _, _, _ = mock_adapter()
        with patch.object(adapter_module.shutil, "which", return_value="C:/mock/Code.exe"):
            result = adapter.execute(Intent("app.open", {"app": "vscode"}))
        self.assertEqual(result["status"], "action_requested")
        adapter.opener.assert_called_once_with(["C:/mock/Code.exe"])

    def test_every_static_alias_reaches_its_mocked_action_route(self):
        chrome = {"hwnd": 2, "pid": 20, "title": "Chrome", "exe": "chrome.exe"}
        ui_routed = {"app.open_keyboard", "voice.start_eye", "voice.calibrate_gaze",
                     "voice.stop_control", "system.emergency_stop", "assistant.quit",
                     "assistant.screenshot", "accessibility.read_selection"}
        for intent_name, phrases in STATIC_PHRASES.items():
            for phrase in phrases:
                with self.subTest(intent=intent_name, phrase=phrase):
                    adapter, _api, _desktop, _audio = mock_adapter([chrome], foreground=chrome,
                                                                     clipboard=types.SimpleNamespace(text=lambda: "", setText=Mock()))
                    executor = Mock(return_value={"status": "executed", "message": "mock UI action"}) if intent_name in ui_routed else adapter.execute
                    dispatcher = CommandDispatcher(executor, cancel_executor=adapter.cancel_all)
                    result = dispatcher.dispatch_transcript(phrase)
                    if result["status"] == "confirmation_required":
                        result = dispatcher.dispatch_transcript("confirm")
                    self.assertIn(result["status"], {"executed", "action_requested", "cancelled", "ambiguous", "unsupported"})
                    if intent_name in ui_routed and result["status"] == "executed":
                        executor.assert_called_once()

    def test_mouse_movements_scroll_drag_and_stop_are_mocked(self):
        adapter, _, desktop, _ = mock_adapter()
        adapter.execute(Intent("mouse.move_up", {}))
        desktop.moveRel.assert_called_once_with(0, -40, duration=0.15, _pause=False)
        adapter.execute(Intent("mouse.scroll_down"))
        desktop.scroll.assert_called_once_with(-5)
        adapter.execute(Intent("mouse.drag_start"))
        self.assertTrue(adapter.dragging)
        adapter.execute(Intent("mouse.stop"))
        self.assertFalse(adapter.dragging)
        desktop.mouseUp.assert_called_once()
        oversized = adapter.execute(Intent("mouse.drag", {"start": (0, 0), "end": (900, 0)}))
        self.assertEqual(oversized["status"], "ambiguous")
        self.assertEqual(desktop.mouseDown.call_count, 1)  # only the prior drag-start action

    def test_keyboard_and_text_actions_use_mock_desktop(self):
        adapter, _, desktop, _ = mock_adapter()
        actions = [
            ("keyboard.enter", "press", "enter"), ("keyboard.escape", "press", "esc"),
            ("keyboard.tab", "press", "tab"), ("keyboard.space", "press", "space"),
            ("keyboard.backspace", "press", "backspace"), ("keyboard.delete", "press", "delete"),
            ("keyboard.up", "press", "up"), ("keyboard.down", "press", "down"),
            ("keyboard.left", "press", "left"), ("keyboard.right", "press", "right"),
        ]
        for name, method, key in actions:
            for mock_name in ("hotkey", "press", "write"): getattr(desktop, mock_name).reset_mock()
            self.assertEqual(adapter.execute(Intent(name))["status"], "action_requested")
            getattr(desktop, method).assert_called_once_with(key)
        for mock_name in ("hotkey", "press", "write"): getattr(desktop, mock_name).reset_mock()
        adapter.execute(Intent("keyboard.redo")); desktop.hotkey.assert_called_once_with("ctrl", "y")
        for mock_name in ("hotkey", "press", "write"): getattr(desktop, mock_name).reset_mock()
        adapter.execute(Intent("keyboard.type_text", {"text": "Hi,  there!"}))
        self.assertEqual(
            desktop.write.call_args_list,
            [call(character, interval=0.01) for character in "Hi,  there!"],
        )
        for mock_name in ("hotkey", "press", "write"): getattr(desktop, mock_name).reset_mock()
        adapter.execute(Intent("text.find", {"query": "needle"}))
        self.assertEqual(desktop.hotkey.call_args, call("ctrl", "f"))
        self.assertEqual(
            desktop.write.call_args_list,
            [call(character, interval=0.01) for character in "needle"],
        )
        desktop.hotkey.reset_mock(); desktop.press.reset_mock(); desktop.write.reset_mock()
        dispatcher = CommandDispatcher(adapter.execute)
        self.assertEqual(dispatcher.dispatch_transcript("replace old with new")["status"], "confirmation_required")
        self.assertEqual(dispatcher.dispatch_transcript("confirm")["status"], "unsupported")
        desktop.hotkey.assert_not_called()
        desktop.write.assert_not_called()

    def test_volume_brightness_and_power_use_injected_system_surfaces(self):
        adapter, _, _, audio = mock_adapter()
        result = adapter.execute(Intent("system.volume_up"))
        audio.set_volume.assert_called_with(24000 + 6554)
        self.assertEqual(result["status"], "executed")
        self.assertIn("increased to", result["message"])
        audio.set_volume.reset_mock()
        result = adapter.execute(Intent("system.volume_down"))
        audio.set_volume.assert_called_with(24000)
        self.assertEqual(result["status"], "executed")
        self.assertIn("decreased to", result["message"])
        audio.set_volume.reset_mock()
        result = adapter.execute(Intent("system.mute")); audio.set_mute.assert_called_with(True)
        self.assertEqual(result["status"], "executed")
        self.assertTrue(audio.state["muted"])
        result = adapter.execute(Intent("system.unmute")); audio.set_mute.assert_called_with(False)
        self.assertEqual(result["status"], "executed")
        self.assertFalse(audio.state["muted"])
        audio.set_volume.assert_not_called()
        brightness = adapter.execute(Intent("system.brightness.set", {"percent": 45}))
        self.assertEqual(brightness["status"], "action_requested")
        self.assertTrue(adapter.shell_run.called)
        # Power actions are only called after dispatcher confirmation; adapter gets a fake launcher.
        adapter.execute(Intent("system.power.shutdown"))
        self.assertEqual(adapter.opener.call_args.args[0], ["shutdown", "/s", "/t", "15"])
        adapter.cancel_all()
        adapter.shell_run.assert_called_with(["shutdown", "/a"], check=False, capture_output=True)

    def test_audio_actions_are_coordinated_and_emergency_stop_rejects_them(self):
        from desktop_input import DesktopInputCoordinator
        coordinator = DesktopInputCoordinator()
        adapter, _, _, audio = mock_adapter(input_coordinator=coordinator)
        self.assertEqual(adapter.execute(Intent("system.volume_up"))["status"], "executed")
        audio.set_volume.reset_mock()
        audio.get_volume.reset_mock()
        coordinator.activate_emergency()
        result = adapter.execute(Intent("system.volume_up"))
        self.assertEqual(result["status"], "failed")
        audio.get_volume.assert_not_called()
        audio.set_volume.assert_not_called()

    def test_audio_adapter_failure_is_reported_as_execution_failure(self):
        adapter, _, _, audio = mock_adapter()
        audio.get_volume.side_effect = OSError("mock endpoint failure")
        feedback = Mock()
        dispatcher = CommandDispatcher(adapter.execute, feedback)
        result = dispatcher.dispatch_transcript("volume up")
        self.assertEqual(result["status"], "execution_failed")
        self.assertNotEqual(result.get("message"), "System command sent.")
        feedback.assert_called()

    def test_audio_noop_setters_are_not_reported_as_success(self):
        adapter, _, _, audio = mock_adapter()
        audio.set_volume.side_effect = None
        volume = adapter.execute(Intent("system.volume_up"))
        self.assertEqual(volume["status"], "failed")
        self.assertIn("did not apply", volume["message"])
        audio.set_mute.side_effect = None
        mute = adapter.execute(Intent("system.mute"))
        self.assertEqual(mute["status"], "failed")
        self.assertIn("did not apply", mute["message"])


if __name__ == "__main__":
    unittest.main()
