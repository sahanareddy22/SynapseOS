import speech_recognition as sr
import pyttsx3
import threading
import webbrowser
import pyautogui
import os
import random
import time
import wikipedia
import urllib.parse
import subprocess
import re
import queue
import difflib


# ============================================================
# TEXT TO SPEECH
# ============================================================

class TTS:
    """Thread-safe Windows text-to-speech using a dedicated worker thread."""

    def __init__(self):
        self.speech_queue = queue.Queue()
        self.ready = threading.Event()
        self.engine_error = None
        self.worker = threading.Thread(
            target=self._speech_worker,
            name="ChittiTTS",
            daemon=True
        )
        self.worker.start()
        # Wait briefly for the speech engine to initialize before accepting commands.
        self.ready.wait(timeout=15)
        if not self.ready.is_set():
            self.engine_error = RuntimeError("TTS initialization timed out")
            print("TTS initialization timed out.")

    def _speech_worker(self):
        engine = None
        com_initialized = False
        try:
            # SAPI5/pyttsx3 on Windows should be initialized and used on this same thread.
            try:
                import pythoncom
                pythoncom.CoInitialize()
                com_initialized = True
            except ImportError:
                pythoncom = None

            try:
                engine = pyttsx3.init(driverName="sapi5")
            except Exception:
                # Fallback to the platform's default driver.
                engine = pyttsx3.init()

            engine.setProperty("rate", 165)
            engine.setProperty("volume", 1.0)

            voices = engine.getProperty("voices") or []
            if voices:
                # Prefer an English voice where Windows exposes one.
                english_voice = None
                for voice in voices:
                    details = (str(getattr(voice, "id", "")) + " " +
                               str(getattr(voice, "name", ""))).lower()
                    if "en-" in details or "english" in details or "david" in details or "zira" in details:
                        english_voice = voice
                        break
                engine.setProperty("voice", (english_voice or voices[0]).id)

            print("Chitti TTS initialized successfully.")
        except Exception as exc:
            self.engine_error = exc
            print("TTS initialization error:", exc)
        finally:
            self.ready.set()

        if engine is None:
            return

        while True:
            text = self.speech_queue.get()
            try:
                if text is None:
                    break
                print("Chitti:", text)
                engine.say(str(text))
                engine.runAndWait()
            except Exception as exc:
                print("TTS playback error:", exc)
            finally:
                self.speech_queue.task_done()

        try:
            engine.stop()
        except Exception:
            pass
        if com_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    def set_voice(self, lang="en"):
        # The engine is owned by the worker thread. Keep the current Windows voice.
        return

    def speak(self, text):
        if not text:
            return
        if self.engine_error:
            print("Cannot speak; TTS is unavailable:", self.engine_error)
            return
        self.speech_queue.put(str(text))

    def shutdown(self):
        if self.worker.is_alive():
            self.speech_queue.put(None)


# Create one shared TTS engine for this module.
# All calls to speak() in VoiceAssistant use this instance.
tts = TTS()


def speak(text):
    """Queue text for Chitti to speak aloud."""
    tts.speak(text)


# ============================================================
# VOICE ASSISTANT
# ============================================================

class VoiceAssistant:

    def __init__(self, wake_word="chitti", on_status=None, on_command=None):

        # Speech recognizer
        self.recognizer = sr.Recognizer()

        # Microphone
        try:
            self.mic = sr.Microphone()
        except Exception as e:
            self.mic = None
            print("Microphone initialization error:", e)

        # Wake word
        self.wake_word = wake_word.lower()
        self.on_status = on_status or (lambda _message: None)
        self.on_command = on_command or (lambda _text: False)

        # Running state
        self.running = False
        self.worker = None

        # Current language
        self.language = "en"

        # Simple conversation memory
        self.memory = []

        # Configure recognizer
        # More tolerant microphone settings; ambient calibration runs at startup.
        self.recognizer.energy_threshold = 200
        self.recognizer.dynamic_energy_threshold = True
        self.recognizer.dynamic_energy_adjustment_damping = 0.15
        self.recognizer.dynamic_energy_ratio = 1.5
        self.recognizer.pause_threshold = 0.8
        self.recognizer.phrase_threshold = 0.3
        self.recognizer.non_speaking_duration = 0.5

    # ========================================================
    # START
    # ========================================================

    def start(self, background=True):

        if self.worker and self.worker.is_alive():
            print("Voice assistant is already running.")
            return

        if self.mic is None:
            print("No microphone available.")
            speak("I couldn't access the microphone.")
            return

        self.running = True
        self.on_status("Voice listener starting")

        if background:
            self.worker = threading.Thread(
                target=self._listen_loop,
                daemon=True
            )
            self.worker.start()

        else:
            self._listen_loop()

    # ========================================================
    # STOP
    # ========================================================

    def stop(self):

        self.running = False

        print("Voice assistant stopped.")
        if self.worker and self.worker.is_alive() and threading.current_thread() is not self.worker:
            self.worker.join(timeout=1)

    # ========================================================
    # LISTEN LOOP
    # ========================================================

    def _listen_loop(self):

        if self.mic is None:
            print("Microphone is not available.")
            return

        try:

            with self.mic as source:

                print("🎤 Adjusting microphone for ambient noise...")

                self.recognizer.adjust_for_ambient_noise(
                    source,
                    duration=2
                )
                print("Microphone energy threshold:", self.recognizer.energy_threshold)
                print("Microphone calibration completed.")

                print("🎤 Chitti Voice Assistant is listening...")
                print("Wake word:", self.wake_word)

                while self.running:

                    try:

                        # Listen for voice
                        audio = self.recognizer.listen(
                            source,
                            timeout=3,
                            phrase_time_limit=7
                        )

                        print("Processing voice...")

                        # Google speech recognition
                        text = self.recognizer.recognize_google(
                            audio
                        )

                        text = text.lower().strip()

                        print("You said:", text)
                        self.on_status("Recognized: {}".format(text))

                        # ------------------------------------------------
                        # Wake word detection
                        # ------------------------------------------------

                        wake_match = re.search(r"\b{}\b".format(re.escape(self.wake_word)), text)
                        if wake_match:
                            text = (text[:wake_match.start()] + " " + text[wake_match.end():]).strip()
                            if not text:
                                speak("Yes?")
                                continue

                        # Process command
                        if text:

                            self._detect_language(text)

                            try:
                                self._handle(text)
                            except Exception as exc:
                                self.on_status("Command failed: {}".format(exc))
                                print("Voice command failed:", repr(exc))

                    except sr.WaitTimeoutError:
                        continue

                    except sr.UnknownValueError:
                        print("Could not understand audio. Please speak clearly and try again.")
                        continue

                    except sr.RequestError as e:
                        print("Speech recognition service error:", e)

                        speak(
                            "Speech recognition service is unavailable."
                        )

                        time.sleep(2)

                    except Exception as e:

                        print(
                            "Voice listening error:",
                            e
                        )

                        continue

        except Exception as e:

            print(
                "Microphone/listening initialization error:",
                e
            )

            speak(
                "I couldn't start the microphone."
            )
        finally:
            self.running = False
            self.on_status("Voice listener stopped")

    # ========================================================
    # LANGUAGE
    # ========================================================

    def _detect_language(self, text):

        """
        googletrans was removed because its old dependency
        requirements conflict with the OpenAI/httpx stack.

        Chitti currently uses English as the default language.
        """

        self.language = "en"

        try:
            tts.set_voice("en")
        except Exception:
            pass

    # ========================================================
    # COMMAND HANDLER
    # ========================================================

    @staticmethod
    def _normalize_command(text):
        normalized = re.sub(r"[^a-z0-9' ]+", " ", text.lower()).strip()
        normalized = re.sub(r"\s+", " ", normalized)
        aliases = {
            "minimise": "minimize window", "minimise window": "minimize window",
            "minimise this window": "minimize window", "minimize this window": "minimize window",
            "maximize this window": "maximize window", "maximise": "maximize window",
            "maximise window": "maximize window", "maximise this window": "maximize window",
            "close this window": "close window", "minimize the window": "minimize window",
            "maximize the window": "maximize window", "open key board": "open keyboard",
            "go to desktop": "show desktop", "switch applications": "switch window",
        }
        if normalized in aliases:
            return aliases[normalized]
        commands = ("minimize window", "maximize window", "restore window", "close window", "show desktop", "switch window", "open keyboard", "start eye tracking", "calibrate gaze", "stop voice control", "emergency stop", "left click", "right click", "double click", "scroll up", "scroll down", "scroll left", "scroll right", "go back", "go forward", "open notepad", "open calculator", "open chrome", "open file explorer", "open settings", "press enter", "press escape", "press backspace", "press delete", "press space", "press tab", "copy", "paste", "cut", "undo", "redo", "select all", "save")
        if normalized in commands or normalized.startswith(("type ", "open ")):
            return normalized
        substitutions = {"minimize": "minimize", "minimise": "minimize", "maximize": "maximize", "maximise": "maximize", "minimizer": "minimize", "maximizer": "maximize", "window": "window", "windows": "window", "desktop": "desktop", "click": "click", "clique": "click", "keyboard": "keyboard", "key board": "keyboard", "calibrate": "calibrate", "gaze": "gaze", "scroll": "scroll", "notepad": "notepad", "calculator": "calculator", "chrome": "chrome", "enter": "enter", "escape": "escape", "backspace": "backspace", "delete": "delete", "space": "space", "switch": "switch"}
        words = normalized.split()
        for index, word in enumerate(words):
            if word in substitutions:
                words[index] = substitutions[word]
                continue
            matches = difflib.get_close_matches(word, substitutions.keys(), n=1, cutoff=0.72)
            if matches:
                words[index] = substitutions[matches[0]]
        normalized = " ".join(words)
        if normalized in ("minimize", "minimize the", "minimize window please"):
            return "minimize window"
        if normalized in ("maximize", "maximize the", "maximize window please"):
            return "maximize window"
        return normalized

    def _handle(self, text):

        text = self._normalize_command(text)

        if not text:
            return

        print("Command:", text)
        started = time.monotonic()
        if self.on_command(text):
            self.on_status("Command complete in {:.0f} ms".format((time.monotonic() - started) * 1000))
            return

        self.on_status("Executing: {}".format(text))

        if text in ("minimize window", "minimise window", "minimize this window", "minimise this window"):
            pyautogui.hotkey("win", "down")
            return
        if text in ("maximize window", "maximise window", "maximize this window", "maximise this window"):
            pyautogui.hotkey("win", "up")
            return
        if text in ("restore window", "restore this window"):
            pyautogui.hotkey("win", "down")
            return
        if text in ("close window", "close this window"):
            pyautogui.hotkey("alt", "f4")
            return
        if text in ("switch window", "next window"):
            pyautogui.hotkey("alt", "tab")
            return
        if text in ("show desktop", "minimize all windows", "minimise all windows"):
            pyautogui.hotkey("win", "d")
            return
        if text in ("copy", "paste", "cut", "undo", "select all", "save", "redo"):
            shortcut = {"select all": "a"}.get(text, text[0])
            pyautogui.hotkey("ctrl", shortcut)
            return
        key_commands = {"press tab": "tab", "press backspace": "backspace", "press delete": "delete", "press space": "space", "press escape": "esc", "press enter": "enter", "press up": "up", "press down": "down", "press left": "left", "press right": "right", "press home": "home", "press end": "end", "page up": "pageup", "page down": "pagedown"}
        if text in key_commands:
            pyautogui.press(key_commands[text])
            return
        if text in ("left click", "click"):
            pyautogui.click()
            return
        if text == "right click":
            pyautogui.click(button="right")
            return
        if text == "double click":
            pyautogui.doubleClick()
            return

        # ====================================================
        # MEMORY
        # ====================================================

        self.memory.append(text)

        # Keep only last 5 commands
        if len(self.memory) > 5:
            self.memory.pop(0)

        # ====================================================
        # EXIT / STOP
        # ====================================================

        if text in [
            "exit",
            "quit",
            "stop chitti",
            "shutdown chitti",
            "close chitti"
        ]:

            speak("Goodbye.")

            self.stop()

            return

        # ====================================================
        # PLAY SONG / VIDEO
        # ====================================================

        if text.startswith("play "):

            query = text.replace(
                "play",
                "",
                1
            ).strip()

            if not query:

                speak("What would you like me to play?")
                return

            speak(
                f"Playing {query}"
            )

            encoded_query = urllib.parse.quote(query)

            youtube_url = (
                "https://www.youtube.com/results"
                "?search_query="
                + encoded_query
            )

            webbrowser.open(youtube_url)

            return

        # ====================================================
        # OPEN YOUTUBE
        # ====================================================

        if (
            "open youtube" in text
            or text == "youtube"
        ):

            speak("Opening YouTube.")

            webbrowser.open(
                "https://www.youtube.com"
            )

            return

        # ====================================================
        # GOOGLE SEARCH
        # ====================================================

        if text.startswith("search for "):

            query = text.replace(
                "search for",
                "",
                1
            ).strip()

            if not query:

                speak("What should I search for?")
                return

            speak(
                f"Searching for {query}"
            )

            search_url = (
                "https://www.google.com/search?q="
                + urllib.parse.quote(query)
            )

            webbrowser.open(search_url)

            return

        # Also support:
        # "search Python"
        if text.startswith("search "):

            query = text.replace(
                "search",
                "",
                1
            ).strip()

            if query:

                speak(
                    f"Searching for {query}"
                )

                search_url = (
                    "https://www.google.com/search?q="
                    + urllib.parse.quote(query)
                )

                webbrowser.open(search_url)

                return

        # ====================================================
        # TIME
        # ====================================================

        if (
            "what's the time" in text
            or "what is the time" in text
            or "tell me the time" in text
            or text == "time"
        ):

            current_time = time.strftime(
                "%I:%M %p"
            )

            speak(
                f"The time is {current_time}"
            )

            return

        # ====================================================
        # DATE
        # ====================================================

        if (
            "what's the date" in text
            or "what is the date" in text
            or "tell me the date" in text
            or text == "date"
        ):

            current_date = time.strftime(
                "%B %d, %Y"
            )

            speak(
                f"Today's date is {current_date}"
            )

            return

        # ====================================================
        # REMEMBER SOMETHING
        # ====================================================

        if text.startswith("remember "):

            item = text.replace(
                "remember",
                "",
                1
            ).strip()

            if item:

                self.memory.append(
                    "Remember: " + item
                )

                speak(
                    "Okay, I will remember that."
                )

            return

        # ====================================================
        # I LIKE
        # ====================================================

        if text.startswith("i like "):

            item = text.replace(
                "i like",
                "",
                1
            ).strip()

            if item:

                self.memory.append(
                    "I like " + item
                )

                speak(
                    "Okay, I'll remember that."
                )

            return

        # ====================================================
        # SHOW MEMORY
        # ====================================================

        if (
            "what's in my memory" in text
            or "what is in my memory" in text
            or "show my memory" in text
        ):

            if self.memory:

                speak(
                    "Here is what I remember."
                )

                for item in self.memory:

                    speak(item)

            else:

                speak(
                    "I don't have anything in memory yet."
                )

            return

        # ====================================================
        # CLEAR MEMORY
        # ====================================================

        if (
            "clear memory" in text
            or "forget everything" in text
        ):

            self.memory.clear()

            speak(
                "Memory cleared."
            )

            return

        # ====================================================
        # TRANSLATION
        # ====================================================

        if text.startswith("translate"):

            speak(
                "Translation is not available in the current version."
            )

            return

        # ====================================================
        # JOKES
        # ====================================================

        if (
            "tell me a joke" in text
            or "tell a joke" in text
            or text == "joke"
        ):

            jokes = [

                "Why don't scientists trust atoms? "
                "Because they make up everything!",

                "Why did the scarecrow win an award? "
                "Because he was outstanding in his field!",

                "Why don't programmers like nature? "
                "It has too many bugs!",

                "Why was the computer cold? "
                "Because it left its Windows open!"

            ]

            speak(
                random.choice(jokes)
            )

            return

        # ====================================================
        # WIKIPEDIA
        # ====================================================

        if text.startswith("who is "):

            name = text.replace(
                "who is",
                "",
                1
            ).strip()

            if not name:

                speak(
                    "Please tell me who you want to know about."
                )

                return

            try:

                speak(
                    f"Searching Wikipedia for {name}"
                )

                info = wikipedia.summary(
                    name,
                    sentences=2
                )

                speak(info)

            except wikipedia.exceptions.DisambiguationError:

                speak(
                    f"There are multiple results for {name}."
                )

            except wikipedia.exceptions.PageError:

                speak(
                    f"I couldn't find information about {name}."
                )

            except Exception as e:

                print(
                    "Wikipedia error:",
                    e
                )

                speak(
                    "Sorry, I couldn't access Wikipedia."
                )

            return

        # ====================================================
        # OPEN CHROME
        # ====================================================

        if (
            "open chrome" in text
            or text == "chrome"
        ):

            speak(
                "Opening Chrome."
            )

            chrome_paths = [

                r"C:\Program Files\Google\Chrome\Application\chrome.exe",

                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",

                os.path.expandvars(
                    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
                )

            ]

            chrome_opened = False

            for chrome_path in chrome_paths:

                if os.path.exists(chrome_path):

                    subprocess.Popen(
                        [chrome_path]
                    )

                    chrome_opened = True

                    break

            if not chrome_opened:

                speak(
                    "Chrome was not found on this computer."
                )

            return

        # ====================================================
        # OPEN NOTEPAD
        # ====================================================

        if (
            "open notepad" in text
            or text == "notepad"
        ):

            speak(
                "Opening Notepad."
            )

            try:

                subprocess.Popen(
                    ["notepad.exe"]
                )

            except Exception:

                speak(
                    "I couldn't open Notepad."
                )

            return

        # ====================================================
        # OPEN CALCULATOR
        # ====================================================

        if (
            "open calculator" in text
            or text == "calculator"
        ):

            speak(
                "Opening Calculator."
            )

            try:

                subprocess.Popen(
                    ["calc.exe"]
                )

            except Exception:

                speak(
                    "I couldn't open Calculator."
                )

            return

        # ====================================================
        # OPEN FILE EXPLORER
        # ====================================================

        if (
            "open file explorer" in text
            or "open explorer" in text
        ):

            speak(
                "Opening File Explorer."
            )

            try:

                subprocess.Popen(
                    ["explorer.exe"]
                )

            except Exception:

                speak(
                    "I couldn't open File Explorer."
                )

            return

        # ====================================================
        # OPEN SETTINGS
        # ====================================================

        if "open settings" in text:

            speak(
                "Opening Settings."
            )

            try:

                os.system(
                    "start ms-settings:"
                )

            except Exception:

                speak(
                    "I couldn't open Settings."
                )

            return

        # ====================================================
        # CALCULATE
        # ====================================================

        if text.startswith("calculate "):

            expression = text.replace(
                "calculate",
                "",
                1
            ).strip()

            try:

                # Only allow basic mathematical characters
                if not re.fullmatch(
                    r"[0-9+\-*/().%\s]+",
                    expression
                ):

                    raise ValueError(
                        "Invalid mathematical expression"
                    )

                result = eval(
                    expression,
                    {
                        "__builtins__": None
                    },
                    {}
                )

                speak(
                    f"The answer is {result}"
                )

            except Exception as e:

                print(
                    "Calculation error:",
                    e
                )

                speak(
                    "Sorry, I couldn't calculate that."
                )

            return

        # ====================================================
        # MOUSE CONTROL
        # ====================================================

        if "move mouse to center" in text:

            try:

                width, height = pyautogui.size()

                pyautogui.moveTo(
                    width // 2,
                    height // 2,
                    duration=0.5
                )

                speak(
                    "Moving the mouse to the center."
                )

            except Exception:

                speak(
                    "I couldn't move the mouse."
                )

            return

        # ====================================================
        # CLICK
        # ====================================================

        if text == "click":

            try:

                pyautogui.click()

                speak(
                    "Clicked."
                )

            except Exception:

                speak(
                    "I couldn't click."
                )

            return

        # ====================================================
        # SCROLL
        # ====================================================

        if "scroll up" in text:

            pyautogui.scroll(5)

            speak(
                "Scrolling up."
            )

            return

        if "scroll down" in text:

            pyautogui.scroll(-5)

            speak(
                "Scrolling down."
            )

            return

        if "scroll left" in text:
            pyautogui.hscroll(-5)
            return

        if "scroll right" in text:
            pyautogui.hscroll(5)
            return

        if text in ("go back", "navigate back"):
            pyautogui.hotkey("alt", "left")
            return

        if text in ("go forward", "navigate forward"):
            pyautogui.hotkey("alt", "right")
            return

        if text.startswith("type "):
            pyautogui.write(text[5:], interval=0.02)
            return

        # ====================================================
        # PRESS ENTER
        # ====================================================

        if (
            text == "press enter"
            or text == "hit enter"
        ):

            pyautogui.press(
                "enter"
            )

            speak(
                "Done."
            )

            return

        # ====================================================
        # PRESS ESCAPE
        # ====================================================

        if (
            text == "press escape"
            or text == "press esc"
        ):

            pyautogui.press(
                "esc"
            )

            speak(
                "Done."
            )

            return

        # ====================================================
        # TAKE SCREENSHOT
        # ====================================================

        if (
            "take screenshot" in text
            or "screenshot" == text
        ):

            try:

                screenshot_dir = os.path.join(
                    os.path.expanduser("~"),
                    "Pictures",
                    "Chitti Screenshots"
                )

                os.makedirs(
                    screenshot_dir,
                    exist_ok=True
                )

                filename = (
                    "screenshot_"
                    + time.strftime(
                        "%Y%m%d_%H%M%S"
                    )
                    + ".png"
                )

                filepath = os.path.join(
                    screenshot_dir,
                    filename
                )

                image = pyautogui.screenshot()

                image.save(filepath)

                speak(
                    "Screenshot saved."
                )

                print(
                    "Screenshot:",
                    filepath
                )

            except Exception as e:

                print(
                    "Screenshot error:",
                    e
                )

                speak(
                    "I couldn't take the screenshot."
                )

            return

        # ====================================================
        # GREETINGS
        # ====================================================

        if text in [
            "hello",
            "hi",
            "hey",
            "hello chitti",
            "hi chitti",
            "hey chitti"
        ]:

            speak(
                random.choice([
                    "Hello! How can I help you?",
                    "Hi! What can I do for you?",
                    "Hello! I'm listening."
                ])
            )

            return

        # ====================================================
        # DEFAULT
        # ====================================================

        speak(
            "Command not recognized."
        )


# ============================================================
# TEST MODE
# ============================================================

if __name__ == "__main__":

    print("=" * 60)
    print("CHITTI VOICE ASSISTANT")
    print("=" * 60)

    assistant = VoiceAssistant(
        wake_word="chitti"
    )

    try:

        assistant.start(
            background=False
        )

    except KeyboardInterrupt:

        print(
            "\nStopping Chitti..."
        )

        assistant.stop()
