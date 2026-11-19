"""Canonical voice command catalog and strict phrase/parameter parser."""
from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Intent:
    name: str
    params: Dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""


STATIC_PHRASES = {
    "window.switch_next": ("switch window", "switch between open windows", "next window", "switch applications", "switch apps"),
    "desktop.show": ("show desktop", "go to desktop", "minimize all windows", "minimise all windows"),
    "browser.tab.new": ("new tab", "open new tab", "open a new tab", "new browser tab"),
    "browser.tab.close_current": ("close tab", "close current tab", "close this tab"),
    "browser.tab.next": ("next tab", "switch tab", "switch to next tab", "next browser tab"),
    "browser.tab.previous": ("previous tab", "switch to previous tab", "previous browser tab"),
    "browser.refresh": ("refresh", "refresh page", "reload page", "reload this page"),
    "browser.back": ("go back", "navigate back", "back one page"),
    "browser.forward": ("go forward", "navigate forward", "forward one page"),
    "mouse.click": ("click", "left click"),
    "mouse.double_click": ("double click", "double-click"),
    "mouse.right_click": ("right click", "right-click"),
    "mouse.scroll_up": ("scroll up", "scroll up the page", "move up the page", "go up the page", "move up", "go up"),
    "mouse.scroll_down": ("scroll down", "scroll down the page", "move down the page", "go down the page", "move down", "go down"),
    "mouse.scroll_left": ("scroll left", "scroll left the page"),
    "mouse.scroll_right": ("scroll right", "scroll right the page"),
    "mouse.move_up": ("move cursor up", "move pointer up", "move mouse up"),
    "mouse.move_down": ("move cursor down", "move pointer down", "move mouse down"),
    "mouse.move_left": ("move cursor left", "move pointer left", "move mouse left"),
    "mouse.move_right": ("move cursor right", "move pointer right", "move mouse right"),
    "mouse.stop": ("stop mouse movement", "stop moving the mouse"), 
    "mouse.drag_start": ("start dragging", "begin dragging", "drag from here"),
    "mouse.drag_drop": ("drop here", "release mouse", "finish dragging", "drag and drop"),
    "keyboard.enter": ("press enter", "hit enter", "enter"),
    "keyboard.escape": ("press escape", "press esc", "escape"),
    "keyboard.tab": ("press tab", "tab key"),
    "keyboard.space": ("press space", "space key"),
    "keyboard.backspace": ("press backspace", "backspace"),
    "keyboard.delete": ("press delete", "delete key"),
    "keyboard.up": ("press up", "press up arrow", "arrow up", "up arrow"),
    "keyboard.down": ("press down", "press down arrow", "arrow down", "down arrow"),
    "keyboard.left": ("press left", "press left arrow", "arrow left", "left arrow"),
    "keyboard.right": ("press right", "press right arrow", "arrow right", "right arrow"),
    "keyboard.copy": ("copy",), "keyboard.paste": ("paste",),
    "keyboard.cut": ("cut",), "keyboard.undo": ("undo",), "keyboard.redo": ("redo",),
    "keyboard.select_all": ("select all",), "keyboard.save": ("save",),
    "keyboard.home": ("go to beginning", "go to beginning of line", "home key", "press home"),
    "keyboard.end": ("go to end", "go to end of line", "end key", "press end"),
    "keyboard.page_up": ("page up",), "keyboard.page_down": ("page down",),
    "text.previous_word": ("move cursor one word left", "move back one word"),
    "text.next_word": ("move cursor one word right", "move forward one word"),
    "text.previous_line": ("move cursor to previous line", "move up one line"),
    "text.next_line": ("move cursor to next line", "move down one line"),
    "text.select_word": ("select word", "select this word"),
    "text.select_line": ("select line", "select this line"),
    "text.delete_word": ("delete previous word", "delete word"),
    "text.delete_line": ("delete line", "delete this line"),
    "text.beginning": ("go to beginning of document", "go to start of document"),
    "text.end": ("go to end of document",),
    "assistant.help": ("help", "what can i say", "show help", "voice help"),
    "assistant.repeat": ("repeat that", "repeat your last response", "say that again"),
    "assistant.cancel": ("cancel", "cancel that", "cancel current action"),
    "assistant.greeting": ("hello", "hi", "hey"),
    "assistant.screenshot": ("take screenshot", "screenshot"),
    "assistant.mouse_center": ("move mouse to center",),
    "assistant.quit": ("exit", "quit", "stop chitti", "shutdown chitti", "close chitti"),
    "assistant.clear_memory": ("clear memory", "forget everything"),
    "assistant.show_memory": ("show my memory", "what is in my memory", "what's in my memory"),
    "assistant.translate": ("translate",),
    "voice.stop_listening": ("stop listening", "pause listening"),
    "voice.start_listening": ("start listening", "resume listening"),
    "accessibility.read_selection": ("read selected text", "speak the selection", "read selection"),
    "assistant.time": ("time", "what time is it", "what is the time", "what's the time", "tell me the time"),
    "assistant.date": ("date", "what is the date", "what's the date", "tell me the date"),
    "assistant.joke": ("joke", "tell me a joke", "tell a joke"),
    "assistant.youtube": ("youtube", "open youtube"),
    "system.volume_up": ("increase volume", "turn volume up", "volume up", "raise volume", "increase sound", "turn sound up", "sound up", "raise sound"),
    "system.volume_down": ("decrease volume", "turn volume down", "volume down", "lower volume", "decrease sound", "turn sound down", "sound down", "lower sound"),
    "system.mute": ("mute", "mute volume", "mute audio", "silent", "turn on silent", "sound of", "sound off", "turn sound off"),
    "system.unmute": ("unmute", "unmute volume", "unmute audio", "turn off silent", "turn of silent", "sound on", "turn sound on"),
    "system.lock": ("lock computer", "lock this computer", "lock workstation"),
    "system.open_settings": ("open settings", "open windows settings"),
    "app.open_keyboard": ("open keyboard", "open key board"),
    "voice.start_eye": ("start eye tracking",),
    "voice.pause_eye": ("pause eye tracking", "pause eye control"),
    "voice.resume_eye": ("resume eye tracking", "resume eye control"),
    "voice.stop_eye": ("stop eye tracking",),
    "voice.calibrate_gaze": ("calibrate gaze",),
    "voice.stop_control": ("stop voice control",),
    "system.emergency_stop": ("emergency stop", "stop everything"),
    "app.minimize": ("minimize window", "minimise window", "minimize this window", "minimise this window", "minimize the window"),
    "app.maximize": ("maximize window", "maximise window", "maximize this window", "maximise this window", "maximize the window"),
    "app.restore": ("restore window", "restore this window"),
    "window.close": ("close this window",),
    "system.power.sleep": ("sleep computer", "put computer to sleep"),
    "system.power.restart": ("restart computer", "restart the computer"),
    "system.power.shutdown": ("shut down computer", "shutdown computer", "turn off computer"),
    "assistant.confirm": ("confirm", "yes confirm", "confirm action"),
    "assistant.reject": ("no", "cancel confirmation", "do not do that"),
}

APP_ALIASES = {
    "chrome": ("chrome", "google chrome"),
    "notepad": ("notepad",),
    "calculator": ("calculator", "calc"),
    "vscode": ("vs code", "visual studio code", "code"),
    "file_explorer": ("file explorer", "explorer"),
}

SHORTCUTS = {
    "copy": "keyboard.copy", "paste": "keyboard.paste", "cut": "keyboard.cut",
    "undo": "keyboard.undo", "redo": "keyboard.redo", "select all": "keyboard.select_all",
    "save": "keyboard.save",
}

_STATIC_LOOKUP = {phrase: intent for intent, phrases in STATIC_PHRASES.items() for phrase in phrases}


def _clean_phrase(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(str.maketrans({"’": "'", "‘": "'", "‐": "-", "‑": "-", "–": "-", "—": "-"}))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9'./:_ -]+", " ", text.casefold())).strip()


def _command_input(text: str) -> str:
    """Remove explicit spoken wrappers without fuzzy matching or changing dictation payloads."""
    value = text.lstrip()
    wake = re.match(r"(?:(?:hey|okay|ok)\s+)?chitti\b[\s,.:;!?-]*", value, re.I)
    if wake:
        value = value[wake.end():]
    for _ in range(2):
        value = re.sub(r"^(?:please|can you|could you|would you|will you)\s+", "", value, flags=re.I)
    return value


def _app_target(text: str) -> Optional[str]:
    text = text.strip()
    if text.startswith("the "):
        text = text[4:]
    for suffix in (" application", " app", " program"):
        if text.endswith(suffix):
            text = text[:-len(suffix)].strip()
            break
    for canonical, names in APP_ALIASES.items():
        if text in names:
            return canonical
    return None


def _normalized_command_forms(command_candidate: str):
    command_raw = unicodedata.normalize("NFKC", command_candidate).translate(str.maketrans({"’": "'", "‘": "'", "‐": "-", "‑": "-", "–": "-", "—": "-"}))
    command_raw = re.sub(r"\s+(?:please|now)[.!?]*$", "", command_raw, flags=re.I)
    command_raw = re.sub(r"[.!?,;:]+$", "", command_raw).strip()
    return command_raw, _clean_phrase(command_raw)


def normalized_command_text(text: str) -> str:
    """Return safe command text for diagnostics; never return a dictated payload."""
    candidate = _command_input(text)
    if re.match(r"^type[ \t]", candidate, flags=re.I):
        return "type <verbatim payload>"
    return _normalized_command_forms(candidate)[1]


def parse_transcript(text: str) -> Optional[Intent]:
    """Parse exact phrases and explicit parameter patterns. Never fuzzy-matches."""
    if not text or not text.strip():
        return None
    raw = text
    command_candidate = _command_input(raw)
    # Dictation payload is intentionally not normalized; keep punctuation and case.
    match = re.match(r"^type[ \t]([\s\S]*)$", command_candidate, flags=re.IGNORECASE)
    if match:
        return Intent("keyboard.type_text", {"text": match.group(1)}, raw)
    command_raw, phrase = _normalized_command_forms(command_candidate)
    if phrase in _STATIC_LOOKUP:
        return Intent(_STATIC_LOOKUP[phrase], {}, raw)
    if phrase in {"maximize", "maximise"}: return Intent("app.maximize", {}, raw)
    if phrase in {"minimize", "minimise"}: return Intent("app.minimize", {}, raw)
    if phrase == "restore": return Intent("app.restore", {}, raw)
    if phrase in {"close app", "close application", "close"}: return Intent("app.close", {}, raw)
    if phrase == "close window": return Intent("window.close", {}, raw)

    match = re.fullmatch(r"(?:search google for|search for|search)\s+(.+)", command_raw, re.I)
    if match:
        return Intent("browser.search", {"query": match.group(1).strip()}, raw)
    match = re.fullmatch(r"(?:go to|navigate to|open website)\s+(\S+)", command_raw, re.I)
    if match:
        return Intent("browser.navigate", {"url": match.group(1).strip()}, raw)
    match = re.fullmatch(r"(?:find|search this document for)\s+(.+)", command_raw, re.I)
    if match:
        return Intent("text.find", {"query": match.group(1).strip()}, raw)
    match = re.fullmatch(r"replace\s+(.+?)\s+with\s+(.+)", command_raw, re.I)
    if match:
        return Intent("text.replace", {"old": match.group(1), "new": match.group(2)}, raw)
    match = re.fullmatch(r"calculate\s+(.+)", command_raw, re.I)
    if match:
        return Intent("assistant.calculate", {"expression": match.group(1)}, raw)
    match = re.fullmatch(r"(?:who is|tell me about)\s+(.+)", command_raw, re.I)
    if match:
        return Intent("assistant.wikipedia", {"subject": match.group(1)}, raw)
    match = re.fullmatch(r"(?:play song|play video|play)\s+(.+)", command_raw, re.I)
    if match: return Intent("assistant.play", {"query": match.group(1).strip()}, raw)
    match = re.fullmatch(r"(?:remember\s+(?:that\s+)?|i like\s+)(.+)", command_raw, re.I)
    if match: return Intent("assistant.remember", {"text": match.group(1)}, raw)
    match = re.fullmatch(r"(?:close app|close application|close)\s+(.+)", phrase)
    if match:
        target = _app_target(match.group(1))
        if target: return Intent("app.close", {"app": target}, raw)
    match = re.fullmatch(r"open app\s+(.+)", phrase)
    if match:
        target = _app_target(match.group(1))
        if target: return Intent("app.open", {"app": target}, raw)
    match = re.fullmatch(r"(?:press )?(?:control|ctrl|command)\s+(?:(?:shift|alt|win|windows)\s+)*(?:[a-z0-9]|f(?:[1-9]|1[0-2]))", phrase)
    if match:
        keys = ["ctrl" if k in {"control", "command"} else "win" if k == "windows" else k for k in match.group(0).replace("press ", "").split()]
        if len(keys) <= 4 and len(set(keys[:-1])) == len(keys[:-1]): return Intent("keyboard.shortcut", {"keys": keys}, raw)

    for verb, intent in (("open", "app.open"), ("launch", "app.open"), ("start", "app.open"),
                         ("switch to", "app.switch"), ("bring up", "app.switch"),
                         ("activate", "app.switch"), ("close app", "app.close"),
                         ("close application", "app.close"), ("close", "app.close"), ("maximize", "app.maximize"),
                         ("maximise", "app.maximize"), ("minimize", "app.minimize"),
                         ("minimise", "app.minimize"), ("restore", "app.restore")):
        prefix = verb + " "
        if phrase.startswith(prefix):
            target = _app_target(phrase[len(prefix):])
            if target:
                return Intent(intent, {"app": target}, raw)
    bare = {"maximize": "app.maximize", "maximise": "app.maximize", "minimize": "app.minimize",
            "minimise": "app.minimize", "restore": "app.restore", "close app": "app.close",
            "close application": "app.close", "close window": "window.close", "close": "app.close"}
    if phrase in bare:
        return Intent(bare[phrase], {}, raw)
    for verb, intent in (("close window named", "window.close"), ("close window", "window.close"),
                         ("minimize window named", "window.minimize"), ("minimize window", "window.minimize"),
                         ("maximize window named", "window.maximize"), ("maximize window", "window.maximize"),
                         ("restore window named", "window.restore"), ("restore window", "window.restore"),
                         ("switch to window named", "window.activate"), ("switch to window", "window.activate")):
        verb_pattern = r"\s+".join(re.escape(word) for word in verb.split())
        match = re.fullmatch(verb_pattern + r"\s+(.+)", command_raw, re.I)
        if match:
            target = match.group(1).strip()
            if target:
                return Intent(intent, {"title": target}, raw)

    match = re.fullmatch(r"(?:increase|decrease) brightness", phrase)
    if match:
        operation = "system.brightness.up" if phrase.startswith("increase") else "system.brightness.down"
        return Intent(operation, {}, raw)
    match = re.fullmatch(r"set brightness to (\d{1,3}) percent", phrase)
    if match:
        return Intent("system.brightness.set", {"percent": int(match.group(1))}, raw)
    match = re.fullmatch(r"(?:drag|drag and drop) from (\d{1,5}) (\d{1,5}) to (\d{1,5}) (\d{1,5})", phrase)
    if match:
        return Intent("mouse.drag", {"start": (int(match[1]), int(match[2])), "end": (int(match[3]), int(match[4]))}, raw)
    match = re.fullmatch(r"move cursor (up|down|left|right)(?: (\d{1,4}) pixels?)?", phrase)
    if match:
        return Intent("mouse.move", {"direction": match[1], "pixels": min(int(match[2] or 40), 200)}, raw)

    # Exact one-word app names are intentionally not treated as implicit opens.
    return None


def command_catalog():
    """Expose a stable, reviewable catalog summary for help and tests."""
    catalog = {name: list(phrases) for name, phrases in STATIC_PHRASES.items()}
    for app, aliases in APP_ALIASES.items():
        catalog[f"app.target.{app}"] = list(aliases)
    catalog.update({
        "app.open": ["open/launch/start <named app>"],
        "app.close": ["close app <named app>", "close application <named app>"],
        "app.switch": ["switch to <named app>", "activate <named app>"],
        "app.minimize": ["minimize <named app>"], "app.maximize": ["maximize <named app>"],
        "app.restore": ["restore <named app>"],
        "window.close": ["close window <title>", "close window named <title>"],
        "window.activate": ["switch to window named <title>"],
        "window.minimize": ["minimize window named <title>"],
        "window.maximize": ["maximize window named <title>"],
        "window.restore": ["restore window named <title>"],
        "browser.search": ["search Google for <query>", "search for <query>"],
        "browser.navigate": ["go to <url>", "navigate to <url>"],
        "keyboard.type_text": ["type <verbatim dictated text>"],
        "text.find": ["find <text>"], "text.replace": ["replace <old> with <new>"],
        "assistant.calculate": ["calculate <arithmetic expression>"],
        "assistant.remember": ["remember <text>"], "assistant.wikipedia": ["who is <subject>"],
        "system.brightness": ["increase/decrease brightness", "set brightness to <percent> percent"],
        "mouse.move": ["move cursor <direction> [pixels]"],
        "mouse.drag": ["drag from <x> <y> to <x> <y>"],
        "system.power": ["sleep computer", "restart computer", "shut down computer (confirmation required)"],
    })
    return catalog
