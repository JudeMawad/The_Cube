#!/usr/bin/env python3

import socket
import io
import os
import subprocess
import time
import wave
from collections import deque

from config.paths import wake_model_path
from display import transport as display_transport
from display.weather import WeatherProvider
from display.music import configured_provider
from control import Controls

from notifications import Notifications

import numpy as np
from openwakeword.model import Model



# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2

# openWakeWord expects 1280 samples = 80 ms at 16 kHz.
FRAME_SAMPLES = 1280
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH

MICROPHONE_DEVICE = os.environ.get(
    "CUBE_CAPTURE_DEVICE",
    "plughw:0,0",
)


# ---------------------------------------------------------------------------
# Wake word
# ---------------------------------------------------------------------------

WAKE_MODELS = [
    wake_model_path(),
]

WAKE_THRESHOLDS = {
    "hey_cube": 0.50,
}


# ---------------------------------------------------------------------------
# Command recording
# ---------------------------------------------------------------------------


COMMAND_START_RMS = 0.010


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

VOICE_ENDPOINT = os.environ.get(
    "CUBE_VOICE_ENDPOINT",
    "http://server:8765/voice",
)

CLIENT_ID = os.environ.get("CUBE_CLIENT_ID", socket.gethostname())
EVENTS_BASE = VOICE_ENDPOINT.rsplit("/", 1)[0]

# ---------------------------------------------------------------------------
# Display presentation
# ---------------------------------------------------------------------------

controls = Controls(EVENTS_BASE, CLIENT_ID)

DISPLAY_SOCKET = socket.socket(
    socket.AF_UNIX,
    socket.SOCK_DGRAM,
)


def send_display_state(message: str) -> None:
    try:
        display_transport.send_state(DISPLAY_SOCKET, message)
    except (
        ConnectionRefusedError,
        FileNotFoundError,
    ):
        pass
    except OSError as error:
        print(
            f"Display control unavailable: {error}",
            flush=True,
        )


def pcm_rms(audio: bytes) -> float:
    samples = np.frombuffer(audio, dtype="<i2")

    if len(samples) == 0:
        return 0.0

    normalized = samples.astype(np.float32) / 32768.0

    return float(
        np.sqrt(
            np.mean(normalized * normalized)
        )
    )


def start_microphone() -> subprocess.Popen:
    command = [
        "arecord",
        "-q",
        "-D",
        MICROPHONE_DEVICE,
        "-f",
        "S16_LE",
        "-r",
        str(SAMPLE_RATE),
        "-c",
        "1",
        "-t",
        "raw",
    ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if process.stdout is None:
        raise RuntimeError(
            "Could not open microphone stream."
        )

    return process


def read_frame(
    process: subprocess.Popen,
) -> bytes:
    if process.stdout is None:
        raise RuntimeError(
            "Microphone stream is unavailable."
        )

    data = process.stdout.read(FRAME_BYTES)

    if len(data) != FRAME_BYTES:
        stderr = ""

        if process.stderr is not None:
            stderr = process.stderr.read().decode(
                "utf-8",
                errors="replace",
            )

        raise RuntimeError(
            "Microphone stream ended unexpectedly. "
            + stderr
        )

    return data


def make_wav(audio: bytes) -> bytes:
    buffer = io.BytesIO()

    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(audio)

    return buffer.getvalue()


def listening_seconds(result):
    if "listen_for_seconds" in result:
        return max(0, min(10, float(result["listen_for_seconds"])))
    if result.get("follow_up") or result.get("action") == "movie_confirmation":
        return max(0, min(10, float(result.get("expires_in", 120))))
    return 0


def wake_triggered(
    predictions: dict,
) -> tuple[bool, str, float]:
    best_name = ""
    best_score = 0.0

    for name, value in predictions.items():
        score = float(value)

        if score > best_score:
            best_name = name
            best_score = score

    threshold = WAKE_THRESHOLDS.get(
        best_name,
        0.20,
    )

    return (
        best_score >= threshold,
        best_name,
        best_score,
    )


def main() -> None:
    from interaction import Coordinator, Interaction
    from voice_runtime import VoiceRuntime
    from audio.activity import ActivityPublisher

    wake_model = Model(wakeword_models=[str(path) for path in WAKE_MODELS],
                       inference_framework="onnx", vad_threshold=0.30)
    microphone = start_microphone()
    notifications = Notifications(EVENTS_BASE, CLIENT_ID)
    try:
        music_display = configured_provider(EVENTS_BASE, CLIENT_ID)
    except ValueError:
        music_display = None
        print("Music display configuration invalid; voice startup continues.", flush=True)
    try:
        weather = WeatherProvider()
    except ValueError:
        weather = None
        print("Weather configuration invalid; weather unavailable.", flush=True)
    runtime = VoiceRuntime(VOICE_ENDPOINT, CLIENT_ID, controls, make_wav, listening_seconds)
    activity = ActivityPublisher()
    activity.start()
    coordinator = Coordinator(runtime.submit, send_display_state, activity=activity.publish)
    noise_floor = 0.004
    noise_samples = deque([noise_floor] * 25, maxlen=125)
    notifications.start()
    controls.start()
    if weather is not None:
        try:
            weather.start()
        except (OSError, RuntimeError):
            weather = None
            print("Weather worker unavailable; voice startup continues.", flush=True)
    coordinator.set_state("idle")
    if music_display is not None:
        try:
            music_display.start()
        except (OSError, RuntimeError):
            music_display = None
            print("Music display worker unavailable; voice startup continues.", flush=True)
    print(f'Microphone: {MICROPHONE_DEVICE}; voice server: {VOICE_ENDPOINT}', flush=True)
    print('Ready. Say "Hey Cube".', flush=True)
    try:
        while True:
            data = read_frame(microphone)
            predictions = wake_model.predict(np.frombuffer(data, dtype="<i2"))
            active, _, _ = wake_triggered(predictions)
            level = pcm_rms(data)
            accepted = coordinator.frame(data, level, active, noise_floor)
            # Wake processing wins over a simultaneous old completion.
            runtime.drain(coordinator, noise_floor)
            if accepted:
                print(f"[BARGE] accepted at={time.monotonic():.6f} generation={coordinator.generation} "
                      f"interaction={coordinator.current.identifier}", flush=True)
            if coordinator.state == "idle" and not active:
                if level < 0.04:
                    noise_samples.append(level)
                    target = max(0.0015, min(0.012, float(np.percentile(noise_samples, 20))))
                    noise_floor += (target - noise_floor) * (0.08 if target < noise_floor else 0.002)
                if level < max(COMMAND_START_RMS, noise_floor * 2.2):
                    item = notifications.take()
                    if item is not None:
                        if coordinator.current is not None:
                            coordinator.current.cancel()
                        coordinator.generation += 1
                        coordinator.current = Interaction(coordinator.generation)
                        coordinator.set_state("thinking")
                        runtime.notification(coordinator.current, notifications, item)
    except KeyboardInterrupt:
        pass
    finally:
        coordinator.close()
        activity.close()
        notifications.close()
        if weather is not None:
            weather.close()
        if music_display is not None:
            music_display.close()
        controls.close()
        runtime.close()
        send_display_state("idle")
        microphone.terminate()
        try:
            microphone.wait(timeout=2)
        except subprocess.TimeoutExpired:
            microphone.kill()
            microphone.wait()


if __name__ == "__main__":
    main()
