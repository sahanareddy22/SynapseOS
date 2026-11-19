# SynapseOS

SynapseOS is a Windows desktop assistant for hands-free computer access. It combines a PyQt5 dashboard, webcam-based gaze and blink input, speech-recognized commands, a floating virtual keyboard, and a shared coordinator for voice and eye-generated desktop input.

The dashboard entry point is `app.py`. Camera processing and speech recognition start only when the matching controls are started. SynapseOS sends keyboard and mouse input to Windows applications, so review each command before enabling it in a sensitive workflow.

## Features

- **Eye tracking:** OpenCV camera capture and MediaPipe Face Mesh landmarks estimate gaze. A live preview can show facial landmarks.
- **Gaze cursor:** Five-point calibration maps gaze to the primary display. Cursor control stays disabled until calibration succeeds. Smoothing, a dead zone, and screen-boundary clamping reduce jitter.
- **Blink input:** A left-eye blink requests a left click, a right-eye blink requests a right click, and a simultaneous bilateral blink requests a double-click. Blink duration and cooldown checks reject long closures and repeated events.
- **Voice control:** Google Speech Recognition transcribes microphone audio; the strict command catalog maps supported phrases to structured actions. Explicit phrases and parameters are used instead of unrestricted fuzzy matching.
- **Virtual keyboard:** A floating, always-on-top Qt keyboard provides character entry and supported special-key/modifier interactions. It captures the foreground target when opened and routes simulated keystrokes back to that target.
- **Voice and eye coordination:** A shared input coordinator serializes desktop actions, gives voice actions priority, and suppresses eye-generated input while voice work is active or waiting.
- **Emergency Stop:** The dashboard button and supported voice command latch emergency stop, cancel further coordinated actions, stop listening, request camera shutdown, and close the virtual keyboard. Restart the application to clear the latch.
- **Dashboard:** Separate Dashboard, Eye Tracking, Voice Control, Activity, and Settings navigation areas use the existing Qt application. The Activity page is currently a placeholder; it is not a persistent activity log.

## Technology

- Python 3.10 (64-bit recommended by the project)
- PyQt5 for the dashboard and virtual keyboard
- OpenCV and MediaPipe for camera frames and Face Mesh
- NumPy for image and gaze data
- SpeechRecognition with Google Web Speech for speech-to-text
- PyAudio for microphone capture
- pyttsx3 for local text-to-speech when the local speech engine is available
- PyAutoGUI for desktop input
- Python standard-library Windows APIs and Core Audio COM calls for window, system, and master-audio control
- `wikipedia` for the Wikipedia summary command

Dependency constraints are listed in [requirements.txt](requirements.txt). Some packages, especially PyAudio and MediaPipe, require wheels compatible with the selected Python and Windows environment.

## System requirements

- Windows with a working webcam, microphone, speakers/headphones, and a primary display.
- Python 3.10, preferably 64-bit.
- Internet access for Google speech recognition and Wikipedia summaries.
- Windows access to the default render audio endpoint for master-volume and mute commands.
- Camera permission for the Python application and microphone permission for voice control.

Camera, microphone, and Windows audio hardware have not been validated on every device. Multi-monitor gaze mapping is not implemented; cursor calibration targets the primary display.

## Installation

From PowerShell in the repository directory:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If PowerShell blocks activation, use the environment interpreter directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If PyAudio installation fails, install a wheel compatible with the installed Python version and architecture using your organization’s normal package process. Do not mix packages from a different Python environment. The repository does not pin every transitive package to an exact version.

## Run

Launch the dashboard:

```powershell
python app.py
```

Start **Eye Tracking** and **Voice Control** separately from the dashboard. To run the standalone OpenCV eye-tracking window instead of the dashboard:

```powershell
python mains.py
```

In the standalone window, press `C` to request calibration and `Q` to quit. The dashboard is the normal entry point for using eye tracking together with voice control and the virtual keyboard.

## Voice Control

Voice recognition is English-oriented and uses the Google Web Speech endpoint. When listening is active, commands may be said directly; the wake word is `Chitti`. After listening has been paused, use the wake word with a supported restart phrase, for example **“Chitti, start listening.”** The dashboard’s Start/Stop Voice Control buttons are also available. **Stop voice control** stops voice listening; it does not stop Eye Tracking.

The examples below are taken from the current command catalog and implementation. Similar phrases are supported only where explicitly listed in `voice/catalog.py`.

### Applications and windows

| Action | Example phrases |
| --- | --- |
| Open a supported app | `open Chrome`, `launch Notepad`, `start Calculator`, `open VS Code`, `open File Explorer` |
| Switch to an app | `switch to Chrome`, `activate Notepad` |
| Minimize/maximize/restore an app | `minimize Calculator`, `maximize Notepad`, `restore Chrome` |
| Close an app | `close app Notepad`, `close application Chrome` |
| Operate a named window | `minimize window named Editor`, `maximize window named Editor`, `restore window named Editor`, `switch to window named Editor` |
| Close a named window | `close window named Editor` |
| Switch windows / show desktop | `switch window`, `next window`, `show desktop` |

App targets include Chrome, Notepad, Calculator, VS Code, and File Explorer. Close-app and close-window actions require confirmation. Named windows must resolve to one unambiguous external window; SynapseOS excludes its own dashboard from named application targeting.

### Browser

Examples: `new tab`, `close tab`, `next tab`, `previous tab`, `refresh page`, `go back`, `go forward`, `search Google for accessible technology`, and `go to https://example.com`.

Browser keyboard shortcuts are sent only after the configured browser is identified and focused. Closing a tab requires confirmation. Search and URL navigation open the configured browser; the resulting page load is not independently verified.

### Mouse and keyboard

- **Mouse:** `click`, `left click`, `right click`, `double click`, `scroll up`, `scroll down`, `scroll left`, `scroll right`, `move cursor left 30 pixels`, and `drag from 100 100 to 200 200`.
- **Keys:** `press enter`, `press escape`, `press tab`, `press space`, `press backspace`, `press delete`, arrow-key phrases, `home key`, `end key`, `page up`, and `page down`.
- **Shortcuts:** `copy`, `paste`, `cut`, `undo`, `redo`, `select all`, and `save`.
- **Text editing:** `move back one word`, `move forward one word`, `move up one line`, `select word`, `select line`, `delete word`, `delete line`, `go to beginning of document`, `go to end of document`, and `find <text>`.
- **Dictation:** `type <text>` sends the text payload through the keyboard route without command alias normalization.

Some catalog entries are intentionally incomplete: `replace <old> with <new>` is recognized but currently returns unsupported. Function-key buttons are visible on the virtual keyboard, but their key activation is not implemented as function-key input. The command catalog is broader than the set of executable commands; an unsupported result means the action is not available in the current adapter.

### System and assistant

- **Audio:** `volume up`, `volume down`, `mute`, and `unmute` adjust or query the Windows default master render endpoint. Volume changes are one approximately 10% step; feedback is based on endpoint read-back.
- **Display and system:** `increase brightness`, `decrease brightness`, `set brightness to 60 percent`, `open settings`, and `lock computer`. Brightness depends on compatible Windows monitor WMI support.
- **Power:** `sleep computer`, `restart computer`, and `shut down computer` require explicit confirmation. Restart and shutdown are scheduled with a 15-second cancellation window.
- **Assistant:** `what time is it`, `tell me the date`, `calculate 12*(3+1)`, `who is Ada Lovelace`, `tell me a joke`, `remember that I like tea`, and `clear memory`.
- **Virtual keyboard:** `open keyboard`.
- **Eye and listening controls:** `start eye tracking`, `pause eye tracking`, `resume eye tracking`, `stop eye tracking`, `calibrate gaze`, `stop listening`, and `start listening`.
- **Emergency Stop:** `emergency stop` or `stop everything`.

Brightness is unsupported on devices without the expected monitor interface. The catalog also contains some assistant phrases whose behavior is unavailable or incomplete (including translation); consult the adapter result rather than assuming every listed catalog phrase executes.

## Eye tracking setup and calibration

1. Start **Eye Tracking** and wait for the dashboard to report that the camera is connected and a valid preview is arriving.
2. Position your face so both eyes are visible. Avoid strong backlighting and keep your head relatively steady while calibrating.
3. Look at the center of the display and activate **Calibrate gaze**.
4. Follow the status prompts through the center, left, right, top, and bottom targets. For each target, look at it steadily and activate **Calibrate gaze** when prompted; keep looking while the camera collects samples.
5. Wait for the **Five-point gaze calibration complete** status before expecting gaze cursor movement. If a target is unstable, hold still and retry the requested target or restart calibration as prompted.

Pausing Eye Tracking suppresses gaze and blink actions while keeping camera capture and preview active. Resume reuses the calibration. Stopping Eye Tracking shuts down the camera pipeline and releases its resources. Calibration accuracy depends on camera placement, lighting, face/eye visibility, and the user; a successful software calibration does not guarantee clinical or universal accuracy.

## Project structure

```text
SynapseOS/
├── app.py                     Main PyQt5 dashboard and service integration
├── mains.py                   Standalone OpenCV eye-tracking entry point
├── vkeyboard.py               Floating virtual keyboard
├── desktop_input.py           Shared voice/eye input coordinator
├── config.py                  Wake-word and legacy configuration values
├── requirements.txt           Python dependencies
├── performance.py             In-memory runtime metric helpers
├── gui/                       Earlier dashboard/keyboard UI modules
├── brain/                     Earlier assistant helper module
├── voice/
│   ├── assistant.py           Microphone, Google ASR, TTS, listener lifecycle
│   ├── catalog.py             Explicit phrases and transcript parser
│   ├── dispatcher.py          Intent validation, confirmation, execution result
│   ├── windows_adapter.py     Windows app, browser, input, audio, and system actions
│   └── gui_request.py         Thread-safe dashboard action requests
├── vision/
│   ├── camera_pipeline.py     Camera worker, preview, calibration, cleanup
│   ├── eye_control.py         Face Mesh, gaze mapping, blink detection/actions
│   ├── frame_utils.py         Canonical camera-frame validation/conversion
│   ├── face_auth.py           Standalone face-detection prototype
│   └── emotion.py             Standalone face-presence/emotion placeholder
├── tests/                     Hardware-free unit and integration tests
├── faces/                     Local image assets used by the project
└── LICENSE                    Project license text
```

`app.py` is the active dashboard entry point. The `gui/`, `brain/`, and duplicate GPT helper modules are older or separate utilities and are not the dashboard’s main voice/eye execution path. The face-authentication module is not integrated into the current dashboard startup flow.

## Tests

The test suite uses `unittest` and mocks camera, voice, UI, and desktop-input boundaries. It is intended to run without a live microphone, webcam, or real desktop actions:

```powershell
python -m unittest discover -s tests -v
```

The mocked suite checks code behavior; it does not verify Windows camera drivers, microphone recognition accuracy, actual audio output, or real desktop interaction on a particular machine.

## Known limitations

- Google speech recognition requires internet access and recognition quality varies with microphone, accent, noise, and phrase duration.
- Dictated audio is sent to Google’s recognition service. Do not dictate passwords, confidential content, or other sensitive information.
- Gaze tracking is calibrated to the primary display only. Multi-monitor mapping is not implemented.
- Eye tracking can be affected by glasses glare, lighting, camera angle, head motion, and individual eye geometry. Calibration and synthetic tests do not establish real-world accuracy.
- Volume and mute operate on the Windows default multimedia render endpoint. Per-application output routing or an exclusive-mode audio stream may use a different endpoint.
- Brightness support depends on monitor hardware and Windows WMI interfaces.
- Some catalog phrases have no implemented action, as noted above. In particular, text replacement and translation are unavailable.
- The Activity page is a placeholder, not a persistent event-history feature.
- `performance.py` computes metrics from measurements supplied by a caller; it does not run a benchmark or provide accuracy guarantees.
- The separate GPT helper files are not part of the main dashboard path, and their API/client setup is not included in `requirements.txt`.

## Privacy and security

- Camera frames are processed locally for tracking and are not saved by the camera pipeline. The application does not record video files.
- Microphone audio is used for live recognition and sent to Google Speech Recognition. The application does not save microphone recordings.
- Voice diagnostics are written to `%LOCALAPPDATA%\SynapseOS\logs\voice-diagnostics.log` on Windows, with rotation. The logger is designed to record pipeline metadata rather than dictated content; recognized text can still be shown live in the Voice Control UI.
- The screenshot voice command saves an image to the user’s Pictures folder. Avoid using it where the screen contains sensitive material.
- Reading a selected-text command temporarily uses the clipboard to obtain the selection, then restores the prior clipboard value.
- Commands such as typing, clicking, closing windows, and power control affect the desktop. Destructive actions require confirmation where implemented. Review the target application before issuing them.
- PyAutoGUI’s fail-safe is enabled by the dashboard entry point; moving the pointer to the upper-left screen corner raises its fail-safe exception.
- Local face images are project data. Keep them private and remove them from any public distribution if consent or ownership is unclear.
- Do not store real credentials in source files. The separate GPT helper modules are not required for the main application and should be reviewed before use.

## Contributors

Contributor names are not recorded in the current repository documentation. Add maintainer and contributor names here when they are provided.

## License

See [LICENSE](LICENSE) for the project’s Apache License 2.0 terms.

## Future improvements

- Add an Activity Center backed by sanitized, session-only events.
- Provide guided calibration targets in the UI and per-user calibration profiles with explicit consent.
- Add a supported function-key path to the virtual keyboard and complete or remove unsupported catalog entries.
- Improve hardware diagnostics for the selected camera, microphone, audio endpoint, and monitor brightness interface.
- Add an explicit settings page for wake-word, speech, camera, and privacy options.
- Expand automated tests for Windows-specific adapters through interface-level mocks and document manual hardware verification separately.
