# SynapseOS

SynapseOS is a Windows-focused hands-free desktop assistant built with Python 3.10, PyQt5, OpenCV, MediaPipe, SpeechRecognition, and PyAutoGUI.

## Setup

Use 64-bit Python 3.10. Install the requirements with `python -m pip install -r requirements.txt`. PyAudio may require a compatible Windows wheel when pip cannot find one for the selected Python build. MediaPipe support is version dependent; use a Python 3.10 compatible wheel.

Run the dashboard with `python app.py`. The camera and microphone start only after their respective controls are selected. Eye tracking uses the front iris landmark; press **Calibrate gaze** while looking at the center of the display. Left-eye blink clicks left, right-eye blink clicks right, and a quick repeat blink of the same eye double-clicks. The on-screen keyboard includes letters, digits, punctuation, function keys, navigation, caps lock, and sticky Shift/Ctrl/Alt/Windows modifiers.

## Voice commands

The recognition service currently uses SpeechRecognition's Google Web Speech endpoint, which requires internet access and sends audio for transcription. Supported commands include `open chrome`, `open notepad`, `open calculator`, `open file explorer`, `open settings`, `click`, `scroll up`, `scroll down`, `scroll left`, `scroll right`, `go back`, `go forward`, `type followed by text`, `press enter`, and `press escape`. Say the configured wake word (`chitti`) with a command or issue a command directly while listening.

## Privacy and safety

Video frames are processed locally and are not saved. Voice transcription uses the online recognition provider. Camera and microphone are accessed only while enabled; the emergency stop closes the camera, stops listening, and closes the keyboard. PyAutoGUI's fail-safe remains enabled: move the pointer to the top-left screen corner to trigger its fail-safe exception.

## Performance evaluation

`performance.py` provides metric collection for gaze error in pixels, exact-match transcription accuracy, command success rate, and response latency. It reports only observations supplied to it; no benchmark or accuracy value is claimed without measured trials. For eye error, collect known target/actual screen coordinates. For voice accuracy, compare recognized text against a manually labeled transcript.

## Current scope

Eye calibration is a center-based gain calibration; per-user multi-point gaze mapping can improve accuracy. A short left-eye blink produces a left click, a short right-eye blink produces a right click, and two same-eye blinks within the gesture interval produce a double click. Voice recognition is English-oriented and depends on an internet connection. The application does not retain audio or video recordings.

Additional spoken desktop commands include `minimize window`, `maximize window`, `restore window`, `close window`, `show desktop`, `switch window`, `copy`, `paste`, `cut`, `undo`, `redo`, `select all`, `save`, `press tab`, `press delete`, `press space`, arrow keys, `page up`, `page down`, `left click`, `right click`, and `double click`. Operating system shortcuts can affect whichever application currently has focus.
