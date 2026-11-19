"""Windows action adapter based on standard-library Win32 calls and PyAutoGUI."""
import ctypes
import datetime
import logging
import os
import shutil
import subprocess
import uuid
import urllib.parse
import ctypes.wintypes as wintypes
import pyautogui
from desktop_input import DEFAULT_INPUT_COORDINATOR, DesktopInputBlocked

logger = logging.getLogger(__name__)

APP_EXE = {"chrome": "chrome.exe", "notepad": "notepad.exe", "calculator": "CalculatorApp.exe", "vscode": "Code.exe", "file_explorer": "explorer.exe"}
APP_LAUNCH_EXE = {"calculator": "calc.exe"}
BROWSER_EXES = {"chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe"}


class _Guid(ctypes.Structure):
    _fields_ = (("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8))

    @classmethod
    def from_string(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


def _com_method(instance, index, result_type, *argument_types):
    """Bind one COM vtable entry without adding a Windows audio dependency."""
    pointer = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[index]
    prototype = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    return prototype(result_type, ctypes.c_void_p, *argument_types)(pointer)


class WindowsMasterAudio:
    """Windows default render-endpoint master volume and mute via Core Audio."""
    CLSID_MMDEVICE_ENUMERATOR = "BCDE0395-E52F-467C-8E3D-C4579291692E"
    IID_MMDEVICE_ENUMERATOR = "A95664D2-9614-4F35-A746-DE8DB63617E6"
    IID_AUDIO_ENDPOINT_VOLUME = "5CDF2C82-841E-4546-9722-0CF74078229A"
    CLSCTX_ALL = 23
    COINIT_MULTITHREADED = 0
    RPC_E_CHANGED_MODE = -2147417850

    @staticmethod
    def _check_hresult(value, operation):
        if value < 0:
            raise OSError("{} failed (HRESULT 0x{:08X})".format(operation, value & 0xFFFFFFFF))

    def _with_endpoint(self, action):
        if os.name != "nt":
            raise OSError("Windows master audio is unavailable on this system.")
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoUninitialize.argtypes = ()
        ole32.CoUninitialize.restype = None
        initialize_result = ole32.CoInitializeEx(None, self.COINIT_MULTITHREADED)
        initialized_here = initialize_result >= 0
        if initialize_result < 0 and initialize_result != self.RPC_E_CHANGED_MODE:
            self._check_hresult(initialize_result, "COM initialization")

        enumerator = ctypes.c_void_p()
        device = ctypes.c_void_p()
        endpoint = ctypes.c_void_p()
        try:
            ole32.CoCreateInstance.argtypes = (ctypes.POINTER(_Guid), ctypes.c_void_p,
                                                ctypes.c_uint32, ctypes.POINTER(_Guid),
                                                ctypes.POINTER(ctypes.c_void_p))
            ole32.CoCreateInstance.restype = ctypes.c_long
            clsid = _Guid.from_string(self.CLSID_MMDEVICE_ENUMERATOR)
            iid = _Guid.from_string(self.IID_MMDEVICE_ENUMERATOR)
            result = ole32.CoCreateInstance(ctypes.byref(clsid), None, self.CLSCTX_ALL,
                                            ctypes.byref(iid), ctypes.byref(enumerator))
            self._check_hresult(result, "Audio device enumerator creation")

            get_default = _com_method(enumerator, 4, ctypes.c_long, ctypes.c_int,
                                      ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
            self._check_hresult(get_default(enumerator, 0, 1, ctypes.byref(device)),
                                "Default audio endpoint lookup")

            activate = _com_method(device, 3, ctypes.c_long, ctypes.POINTER(_Guid),
                                   ctypes.c_uint32, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_void_p))
            endpoint_iid = _Guid.from_string(self.IID_AUDIO_ENDPOINT_VOLUME)
            self._check_hresult(activate(device, ctypes.byref(endpoint_iid), self.CLSCTX_ALL,
                                         None, ctypes.byref(endpoint)),
                                "Master audio endpoint activation")
            return action(endpoint)
        finally:
            for interface in (endpoint, device, enumerator):
                if interface:
                    try:
                        _com_method(interface, 2, ctypes.c_ulong)(interface)
                    except Exception:
                        pass
            if initialized_here:
                ole32.CoUninitialize()

    def get_volume(self):
        def read(endpoint):
            value = ctypes.c_float()
            method = _com_method(endpoint, 9, ctypes.c_long, ctypes.POINTER(ctypes.c_float))
            self._check_hresult(method(endpoint, ctypes.byref(value)), "Master volume read")
            return min(65535, max(0, round(value.value * 65535)))
        return self._with_endpoint(read)

    def set_volume(self, value):
        scalar = min(65535, max(0, int(value))) / 65535.0
        def write(endpoint):
            method = _com_method(endpoint, 7, ctypes.c_long, ctypes.c_float, ctypes.c_void_p)
            self._check_hresult(method(endpoint, scalar, None), "Master volume update")
        return self._with_endpoint(write)

    def get_mute(self):
        def read(endpoint):
            muted = ctypes.c_int()
            method = _com_method(endpoint, 15, ctypes.c_long, ctypes.POINTER(ctypes.c_int))
            self._check_hresult(method(endpoint, ctypes.byref(muted)), "Master mute state read")
            return bool(muted.value)
        return self._with_endpoint(read)

    def set_mute(self, muted):
        def write(endpoint):
            method = _com_method(endpoint, 14, ctypes.c_long, ctypes.c_int, ctypes.c_void_p)
            self._check_hresult(method(endpoint, int(bool(muted)), None), "Master mute state update")
        return self._with_endpoint(write)


# Preserve the earlier import name while routing it to the master endpoint API.
WinMmAudio = WindowsMasterAudio


class Win32WindowApi:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE, WM_CLOSE = 6, 3, 9, 0x10

    def __init__(self):
        if os.name != "nt": raise OSError("Named window control is available only on Windows.")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.LockWorkStation.restype = wintypes.BOOL
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def windows(self):
        found = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def visit(hwnd, _):
            if not self.user32.IsWindowVisible(hwnd): return True
            length = self.user32.GetWindowTextLengthW(hwnd)
            if not length: return True
            title = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, title, length + 1)
            pid = ctypes.c_ulong()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            path = ctypes.create_unicode_buffer(32768)
            process = self.kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            exe = ""
            if process:
                size = ctypes.c_ulong(len(path))
                if self.kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                    exe = os.path.basename(path.value).casefold()
                self.kernel32.CloseHandle(process)
            found.append({"hwnd": int(hwnd), "pid": pid.value, "title": title.value, "exe": exe})
            return True
        self.user32.EnumWindows(callback_type(visit), 0)
        return found

    def foreground(self):
        hwnd = int(self.user32.GetForegroundWindow() or 0)
        return next((w for w in self.windows() if w["hwnd"] == hwnd), None)

    def activate(self, window):
        self.user32.ShowWindow(window["hwnd"], self.SW_RESTORE)
        self.user32.SetForegroundWindow(window["hwnd"])
        active = self.foreground()
        return bool(active and active["hwnd"] == window["hwnd"])

    def operate(self, window, operation):
        if operation == "close": return bool(self.user32.PostMessageW(window["hwnd"], self.WM_CLOSE, 0, 0))
        mode = {"minimize": self.SW_MINIMIZE, "maximize": self.SW_MAXIMIZE, "restore": self.SW_RESTORE}.get(operation)
        if mode is None: return False
        self.user32.ShowWindow(window["hwnd"], mode)
        return bool(self.user32.IsWindow(window["hwnd"]))

    def lock(self): return bool(self.user32.LockWorkStation())
    def screen_size(self): return self.user32.GetSystemMetrics(0), self.user32.GetSystemMetrics(1)


class WindowsControlAdapter:
    def __init__(self, api=None, desktop=None, opener=subprocess.Popen, shell_run=subprocess.run,
                 browser_open=None, default_browser="chrome", own_pid=None, clipboard=None, audio=None,
                 input_coordinator=None):
        self.api = api if api is not None else Win32WindowApi()
        self.desktop = desktop if desktop is not None else pyautogui
        self.input_coordinator = input_coordinator or DEFAULT_INPUT_COORDINATOR
        self.opener, self.shell_run = opener, shell_run
        self.browser_open = browser_open
        self.default_browser = default_browser
        self.own_pid = os.getpid() if own_pid is None else own_pid
        self.dragging = False
        self._power_scheduled = False
        self.audio = audio if audio is not None else (WinMmAudio() if os.name == "nt" else None)
        self.clipboard = clipboard
        self.memory = []

    @staticmethod
    def result(status, message): return {"status": status, "message": message}

    def _windows(self):
        # The dashboard runs in this process. Do not filter by title: a real
        # external application or browser page can legitimately contain that text.
        return [w for w in self.api.windows() if w.get("pid") != self.own_pid]

    def _app_windows(self, app):
        exe = APP_EXE[app].casefold()
        candidates = self._windows()
        matches = [w for w in candidates if w.get("exe", "").casefold() == exe]
        # Modern Windows exposes Calculator's visible UWP window through
        # ApplicationFrameHost.exe rather than CalculatorApp.exe.
        if app == "calculator":
            matches.extend(w for w in candidates if w.get("exe", "").casefold() == "applicationframehost.exe"
                           and "calculator" in w.get("title", "").casefold())
        return matches

    def cancel_all(self):
        if self.dragging:
            self.input_coordinator.schedule_cleanup(self._release_drag_if_active)
        if self._power_scheduled:
            self.shell_run(["shutdown", "/a"], check=False, capture_output=True)
            self._power_scheduled = False

    def _release_drag_if_active(self):
        if not self.dragging:
            return
        try:
            self.desktop.mouseUp()
        finally:
            self.dragging = False

    def _input(self, method, *args, **kwargs):
        # A release remains allowed after cancellation so drag finally blocks
        # can always return the physical mouse button to the up state.
        if method == "mouseUp":
            self.input_coordinator.check_mouse_release()
        else:
            self.input_coordinator.check_voice_action()
        return getattr(self.desktop, method)(*args, **kwargs)

    def keyboard_input_in_transaction(self, method, *args, **kwargs):
        """Send keyboard input only while this thread owns a shared transaction."""
        if method not in {"press", "write", "hotkey"}:
            raise ValueError("Unsupported virtual-keyboard input method.")
        if not self.input_coordinator.current_thread_owns_voice_transaction():
            raise DesktopInputBlocked("Virtual-keyboard input requires the shared input coordinator.")
        return self._input(method, *args, **kwargs)

    def stop_mouse(self):
        if self.dragging:
            if not self.input_coordinator.current_thread_owns_voice_transaction():
                return self.input_coordinator.schedule_cleanup(self._release_drag_if_active)
            try: self._input("mouseUp")
            finally: self.dragging = False

    def execute(self, intent):
        name = intent.name
        if self.input_coordinator.emergency_active:
            return self.result("failed", "Emergency stop is active; desktop input was rejected.")
        input_transaction = (
            name.startswith(("mouse.", "keyboard.", "text."))
            or name in {"desktop.show", "assistant.mouse_center"}
            or name in {"system.volume_up", "system.volume_down", "system.mute", "system.unmute"}
        )
        if not input_transaction:
            return self._execute(intent)
        try:
            return self.input_coordinator.run_voice_transaction(lambda: self._execute(intent))
        except DesktopInputBlocked as exc:
            return self.result("failed", str(exc))

    def _execute(self, intent):
        name, p = intent.name, intent.params
        if name == "app.open": handler = "open_app"
        elif name.startswith("app."): handler = "Win32WindowApi.operate"
        elif name == "window.switch_next": handler = "Win32WindowApi.activate"
        elif name.startswith("window."): handler = "Win32WindowApi.activate/operate"
        elif name.startswith("browser."): handler = "_browser"
        elif name == "desktop.show": handler = "PyAutoGUI.hotkey"
        elif name.startswith("mouse."): handler = "PyAutoGUI mouse action"
        elif name.startswith("keyboard."): handler = "_keyboard"
        elif name.startswith("text."): handler = "_text"
        elif name.startswith("system."): handler = "_system"
        elif name.startswith("assistant."): handler = "_assistant"
        else: handler = "unsupported handler"
        logger.info("ADAPTER selected_action=%s handler=%s", name, handler)
        if name == "app.open": return self.open_app(p["app"])
        if name == "app.switch":
            matches = self._app_windows(p["app"])
            if len(matches) != 1: return self.result("ambiguous" if matches else "unsupported", "Application window is ambiguous or unavailable.")
            ok = self.api.activate(matches[0])
            return self.result("executed" if ok else "failed", "Application focused." if ok else "Could not focus application.")
        if name.startswith("app."):
            matches = self._app_windows(p["app"])
            if not matches: return self.result("unsupported", "No matching application window was found.")
            if name == "app.close" and len(matches) != 1:
                return self.result("ambiguous", "More than one window belongs to that application; name one window explicitly.")
            op = name.split(".")[1]
            failed = [w for w in matches if not self.api.operate(w, op)]
            return self.result("failed" if failed else "action_requested", "Application action requested; Windows completion was not verified." if not failed else "Some windows rejected the action.")
        if name == "window.switch_next":
            windows = self._windows()
            if not windows: return self.result("unsupported", "No other application windows are available.")
            foreground = self.api.foreground()
            index = next((i for i, w in enumerate(windows) if foreground and w["hwnd"] == foreground["hwnd"]), -1)
            target = windows[(index + 1) % len(windows)]
            ok = self.api.activate(target)
            return self.result("executed" if ok else "failed", "Switched to {}.".format(target["title"]) if ok else "Could not switch windows.")
        if name.startswith("window."):
            matches = [w for w in self._windows() if p["title"].casefold() in w["title"].casefold()]
            if not matches: return self.result("unsupported", "No matching window title was found.")
            if len(matches) != 1: return self.result("ambiguous", "More than one window matched; use a longer title.")
            op = name.split(".")[1]
            ok = self.api.activate(matches[0]) if op == "activate" else self.api.operate(matches[0], op)
            return self.result("action_requested" if ok else "failed", "Window action requested; resulting state was not verified." if ok else "Window action failed.")
        if name.startswith("browser."): return self._browser(intent)
        if name == "desktop.show": self._input("hotkey", "win", "d")
        elif name == "mouse.click": self._input("click")
        elif name == "mouse.double_click": self._input("doubleClick")
        elif name == "mouse.right_click": self._input("click", button="right")
        elif name.startswith("mouse.scroll_"):
            d = name.rsplit("_", 1)[1]
            if d in {"up", "down"}: self._input("scroll", 5 if d == "up" else -5)
            else: self._input("hscroll", -5 if d == "left" else 5)
        elif name == "mouse.move" or name.startswith("mouse.move_"):
            d = p.get("direction") or name.rsplit("_", 1)[1]
            n = min(max(p.get("pixels", 40), 1), 200)
            delta = {"up": (0, -n), "down": (0, n), "left": (-n, 0), "right": (n, 0)}[d]
            if self.dragging:
                x, y = self._input("position")
                if max(abs(x + delta[0] - self.drag_origin[0]), abs(y + delta[1] - self.drag_origin[1])) > 600:
                    self.stop_mouse()
                    return self.result("ambiguous", "Drag movement exceeds the 600-pixel safety bound.")
            self._input("moveRel", *delta, duration=0.15, _pause=False)
        elif name == "mouse.stop": self.stop_mouse()
        elif name == "mouse.drag_start":
            if self.dragging: return self.result("ambiguous", "A drag is already active.")
            self.dragging = True
            try:
                self._input("mouseDown")
                self.drag_origin = self._input("position")
            except Exception:
                self.stop_mouse()
                raise
        elif name == "mouse.drag_drop":
            if not self.dragging: return self.result("unsupported", "No drag is active.")
            try: self._input("mouseUp")
            finally: self.dragging = False
        elif name == "mouse.drag":
            width, height = self.api.screen_size()
            x1, y1 = p["start"]; x2, y2 = p["end"]
            x1, x2 = min(max(x1, 0), width - 1), min(max(x2, 0), width - 1)
            y1, y2 = min(max(y1, 0), height - 1), min(max(y2, 0), height - 1)
            if abs(x2-x1) > 600 or abs(y2-y1) > 600: return self.result("ambiguous", "Drag exceeds the 600-pixel safety bound.")
            self._input("moveTo", x1, y1, duration=0.1)
            self.dragging = True
            try:
                self._input("mouseDown")
                self._input("moveTo", x2, y2, duration=0.3)
            finally:
                try: self._input("mouseUp")
                finally: self.dragging = False
        elif name.startswith("keyboard."): self._keyboard(name, p)
        elif name.startswith("text."):
            text_result = self._text(name, p)
            if text_result is not None: return text_result
        elif name.startswith("system."): return self._system(name, p)
        elif name.startswith("assistant."): return self._assistant(name, p)
        elif name == "accessibility.read_selection": return self.result("unsupported", "Reading selected text requires an accessible selection provider; none is configured.")
        else: return self.result("unsupported", "This command is not supported.")
        return self.result("action_requested", "Action sent; the active application response was not verified.")

    def open_app(self, app, args=()):
        exe = APP_LAUNCH_EXE.get(app, APP_EXE[app])
        path = shutil.which(exe)
        if not path and app in {"notepad", "calculator", "file_explorer"}: path = exe
        if not path and app in {"chrome", "vscode"}:
            roots = [os.environ.get("PROGRAMFILES", "C:\\Program Files"), os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"), os.environ.get("LOCALAPPDATA", "")]
            suffix = os.path.join("Google", "Chrome", "Application", exe) if app == "chrome" else os.path.join("Programs", "Microsoft VS Code", exe)
            for root in roots:
                candidate = os.path.join(root, suffix)
                if os.path.isfile(candidate): path = candidate; break
        if not path and app == "vscode": path = shutil.which("code")
        if not path: return self.result("unsupported", "{} was not found on this computer.".format(app))
        self.opener([path, *args])
        return self.result("action_requested", "Launch requested for {}; application startup was not verified.".format(app))

    def _browser_active(self):
        expected = BROWSER_EXES.get(self.default_browser)
        if not expected: return False
        windows = [w for w in self._windows() if w.get("exe", "").casefold() == expected]
        if not windows: return False
        foreground = self.api.foreground()
        if not foreground or foreground.get("exe", "").casefold() != expected:
            if not self.api.activate(windows[0]): return False
        foreground = self.api.foreground()
        return bool(foreground and foreground.get("exe", "").casefold() == expected)

    def _browser(self, intent):
        name, p = intent.name, intent.params
        if name in {"browser.navigate", "browser.search"}:
            url = ("https://www.google.com/search?q=" + urllib.parse.quote_plus(p["query"])) if name == "browser.search" else p["url"]
            return self.open_app(self.default_browser, [url])
        if not self._browser_active(): return self.result("failed", "Browser shortcut cancelled because the configured browser could not be focused.")
        keys = {"browser.tab.new": ("ctrl", "t"), "browser.tab.close_current": ("ctrl", "w"),
                "browser.tab.next": ("ctrl", "tab"), "browser.tab.previous": ("ctrl", "shift", "tab"),
                "browser.refresh": ("ctrl", "r"), "browser.back": ("alt", "left"), "browser.forward": ("alt", "right")}
        if name not in keys: return self.result("unsupported", "Browser command is not supported.")
        def send_shortcut():
            if not self._browser_active():
                return self.result("failed", "Browser shortcut cancelled because the configured browser lost focus.")
            self._input("hotkey", *keys[name])
            return self.result("action_requested", "Browser shortcut sent to {}; resulting page state was not verified.".format(self.default_browser))
        try:
            return self.input_coordinator.run_voice_transaction(send_shortcut)
        except DesktopInputBlocked as exc:
            return self.result("failed", str(exc))

    def _keyboard(self, name, p):
        keymap = {"keyboard.enter":"enter", "keyboard.escape":"esc", "keyboard.tab":"tab", "keyboard.space":"space", "keyboard.backspace":"backspace", "keyboard.delete":"delete", "keyboard.up":"up", "keyboard.down":"down", "keyboard.left":"left", "keyboard.right":"right", "keyboard.home":"home", "keyboard.end":"end", "keyboard.page_up":"pageup", "keyboard.page_down":"pagedown"}
        combos = {"keyboard.copy":("ctrl","c"), "keyboard.paste":("ctrl","v"), "keyboard.cut":("ctrl","x"), "keyboard.undo":("ctrl","z"), "keyboard.redo":("ctrl","y"), "keyboard.select_all":("ctrl","a"), "keyboard.save":("ctrl","s")}
        if name == "keyboard.type_text":
            for character in p["text"]:
                self._input("write", character, interval=0.01)
        elif name in keymap: self._input("press", keymap[name])
        elif name in combos: self._input("hotkey", *combos[name])
        elif name == "keyboard.shortcut": self._input("hotkey", *p["keys"])
        else: raise ValueError("Unknown keyboard intent")

    def _text(self, name, p):
        combos = {"text.previous_word":("ctrl","left"), "text.next_word":("ctrl","right"), "text.previous_line":("up",), "text.next_line":("down",), "text.beginning":("ctrl","home"), "text.end":("ctrl","end"), "text.select_word":("ctrl","shift","right"), "text.delete_word":("ctrl","backspace")}
        if name in combos: self._input("hotkey", *combos[name])
        elif name == "text.select_line": self._input("press", "home"); self._input("hotkey", "shift", "end")
        elif name == "text.delete_line": self._input("press", "home"); self._input("hotkey", "shift", "end"); self._input("press", "backspace")
        elif name == "text.find":
            self._input("hotkey", "ctrl", "f")
            for character in p["query"]:
                self._input("write", character, interval=0.01)
        elif name == "text.replace":
            return self.result("unsupported", "Replace is unavailable until the active application's replacement controls can be identified safely.")
        else: raise ValueError("Unknown text intent")

    def _assistant(self, name, p):
        import datetime
        if name == "assistant.time": return self.result("executed", datetime.datetime.now().strftime("It is %I:%M %p."))
        if name == "assistant.date": return self.result("executed", datetime.date.today().strftime("Today is %B %d, %Y."))
        if name == "assistant.calculate":
            import ast
            import operator
            operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                          ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
                          ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos,
                          ast.Pow: operator.pow}
            def calculate(node):
                if isinstance(node, ast.Expression): return calculate(node.body)
                if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)): return node.value
                if isinstance(node, ast.UnaryOp) and type(node.op) in operations: return operations[type(node.op)](calculate(node.operand))
                if isinstance(node, ast.BinOp) and type(node.op) in operations:
                    left, right = calculate(node.left), calculate(node.right)
                    if abs(left) > 10**12 or abs(right) > 12: raise ValueError("Operand too large")
                    return operations[type(node.op)](left, right)
                raise ValueError("Unsupported arithmetic expression")
            expression = p["expression"]
            tree = ast.parse(expression, mode="eval")
            value = calculate(tree)
            return self.result("executed", "The answer is {}.".format(value))
        if name == "assistant.repeat": return self.result("executed", "Repeat the previous response.")
        if name == "assistant.help": return self.result("executed", "You can control named apps and windows, browser tabs, mouse, keyboard, text editing, system settings, and accessibility features.")
        if name == "assistant.wikipedia":
            import wikipedia
            return self.result("executed", wikipedia.summary(p["subject"], sentences=2))
        if name == "assistant.youtube":
            return self.open_app(self.default_browser, ["https://www.youtube.com"])
        if name == "assistant.play":
            url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(p["query"])
            return self.open_app(self.default_browser, [url])
        if name == "assistant.joke": return self.result("executed", "Why don't programmers like nature? It has too many bugs.")
        if name == "assistant.greeting": return self.result("executed", "Hello! How can I help you?")
        if name == "assistant.remember":
            self.memory.append(p["text"]); self.memory[:] = self.memory[-5:]
            return self.result("executed", "I will remember that for this session.")
        if name == "assistant.show_memory": return self.result("executed", "; ".join(self.memory) if self.memory else "There is nothing in session memory.")
        if name == "assistant.clear_memory":
            self.memory.clear(); return self.result("executed", "Session memory cleared.")
        if name == "assistant.translate": return self.result("unsupported", "Translation is not available.")
        if name == "assistant.mouse_center":
            width, height = self._input("size"); self._input("moveTo", width // 2, height // 2, duration=0.3)
            return self.result("executed", "Pointer moved to the screen center.")
        if name == "assistant.screenshot":
            folder = os.path.join(os.path.expanduser("~"), "Pictures", "SynapseOS Screenshots")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, "screenshot_{}.png".format(datetime.datetime.now().strftime("%Y%m%d_%H%M%S")))
            self.desktop.screenshot().save(path)
            return self.result("executed", "Screenshot saved.")
        if name == "accessibility.read_selection":
            if self.clipboard is None: return self.result("unsupported", "Selected-text reading is unavailable without a clipboard provider.")
            previous = self.clipboard.text()
            try:
                self.desktop.hotkey("ctrl", "c")
                selected = self.clipboard.text()
            finally:
                self.clipboard.setText(previous)
            return self.result("executed", selected or "No text is selected.")
        return self.result("unsupported", "Assistant command unavailable.")

    def _system(self, name, p):
        if name == "system.open_settings":
            self.opener(["explorer.exe", "ms-settings:"])
            return self.result("action_requested", "Windows Settings launch requested; window appearance was not verified.")
        if name == "system.lock":
            ok = self.api.lock()
            return self.result("action_requested" if ok else "failed", "Lock request accepted; workstation state was not independently verified." if ok else "Could not lock the computer.")
        if name.startswith("system.power."):
            commands = {"system.power.sleep":["rundll32.exe","powrprof.dll,SetSuspendState","0,1,0"], "system.power.restart":["shutdown","/r","/t","15"], "system.power.shutdown":["shutdown","/s","/t","15"]}
            self.opener(commands[name]); self._power_scheduled = name != "system.power.sleep"
            return self.result("action_requested", "Power action requested; restart and shutdown have a 15-second Windows cancellation window.")
        if name in {"system.volume_up", "system.volume_down", "system.mute", "system.unmute"}:
            if self.audio is None: return self.result("unsupported", "Audio controls are unavailable on this system.")
            if name in {"system.mute", "system.unmute"}:
                target = name == "system.mute"
                if bool(self.audio.get_mute()) == target:
                    return self.result("executed", "System is already muted." if target else "System is already unmuted.")
                self.audio.set_mute(target)
                actual = bool(self.audio.get_mute())
                logger.info("SYSTEM_AUDIO action=%s requested=%s verified=%s", name, target, actual)
                if actual != target:
                    return self.result("failed", "Windows did not apply the requested system mute state.")
                return self.result("executed", "System mute enabled." if target else "System mute disabled.")

            current = int(self.audio.get_volume())
            step = 6554  # One clear 10% step on the Windows master-volume scale.
            target = min(65535, current + step) if name == "system.volume_up" else max(0, current - step)
            if target == current:
                message = "Master volume is already at maximum." if name == "system.volume_up" else "Master volume is already at minimum."
                return self.result("executed", message)
            self.audio.set_volume(target)
            actual = int(self.audio.get_volume())
            logger.info("SYSTEM_AUDIO action=%s requested=%s verified=%s", name, target, actual)
            if (abs(actual - target) > 256
                    or (name == "system.volume_up" and actual <= current)
                    or (name == "system.volume_down" and actual >= current)):
                return self.result("failed", "Windows did not apply the requested master-volume change.")
            percent = round(actual * 100 / 65535)
            message = "Master volume increased to {}%." if name == "system.volume_up" else "Master volume decreased to {}%."
            return self.result("executed", message.format(percent))
        elif name.startswith("system.brightness"):
            percent = p.get("percent")
            if percent is None:
                script = "(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness | Select-Object -First 1 -ExpandProperty CurrentBrightness)"
                read = self.shell_run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=False, capture_output=True, text=True)
                try: current = int(read.stdout.strip())
                except (ValueError, AttributeError): return self.result("unsupported", "This device does not expose monitor brightness controls.")
                percent = min(100, current + 10) if name == "system.brightness.up" else max(0, current - 10)
            script = "$m=Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods; if(-not $m){{exit 2}}; $m.WmiSetBrightness(1,{})".format(percent)
            proc = self.shell_run(["powershell","-NoProfile","-NonInteractive","-Command",script],check=False,capture_output=True)
            if proc.returncode: return self.result("unsupported", "This device does not expose brightness controls.")
            return self.result("action_requested", "Brightness change requested; final brightness was not verified.")
        else: return self.result("unsupported", "System feature unavailable.")
        return self.result("executed", "System command sent.")
