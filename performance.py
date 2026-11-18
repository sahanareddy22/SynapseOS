"""Small, dependency-free helpers for measuring observed runtime quality."""
import time


class PerformanceMetrics:
    def __init__(self):
        self.eye_errors = []
        self.voice_trials = 0
        self.voice_correct = 0
        self.command_trials = 0
        self.command_successes = 0
        self.response_times_ms = []

    def record_eye_error(self, target, actual):
        self.eye_errors.append(((target[0] - actual[0]) ** 2 + (target[1] - actual[1]) ** 2) ** 0.5)

    def record_voice(self, expected, recognized):
        self.voice_trials += 1
        self.voice_correct += int(expected.strip().lower() == recognized.strip().lower())

    def record_command(self, succeeded, elapsed_seconds):
        self.command_trials += 1
        self.command_successes += int(bool(succeeded))
        self.response_times_ms.append(max(0.0, elapsed_seconds * 1000))

    def summary(self):
        mean = lambda values: sum(values) / len(values) if values else None
        accuracy = lambda good, total: good / total if total else None
        return {
            "eye_error_px_mean": mean(self.eye_errors),
            "voice_accuracy": accuracy(self.voice_correct, self.voice_trials),
            "command_accuracy": accuracy(self.command_successes, self.command_trials),
            "response_time_ms_mean": mean(self.response_times_ms),
            "samples": {"eye": len(self.eye_errors), "voice": self.voice_trials, "command": self.command_trials},
        }


def monotonic_time():
    return time.monotonic()
