"""Speech configuration, player identity and the local progress cue."""
import json
import math
import os
import struct
import wave

from config.paths import APP_DIR, piper_voice_path

PIPER = APP_DIR / ".venv/bin/piper"
VOICE = piper_voice_path()
PW_PLAY = "/usr/bin/pw-play"
TTS_ENDPOINT = os.environ.get("CUBE_TTS_ENDPOINT", "http://server:8765/tts")
TTS_TIMEOUT = 15


def pw_play_args(path):
    """Identify Cube speech/cues without inheriting saved Music stream gain."""
    # WirePlumber keys stream state by media.role before application identity.
    # Also bypass restoration of any older Communication volume/mute state.
    return [
        PW_PLAY,
        "--target", "cube.assistant",
        "--media-role", "Communication",
        "--properties", json.dumps({
            "application.name": "Cube Speech",
            "application.id": "cube.speech",
            "node.name": "cube.speech",
            "state.restore-props": False,
            "state.restore-target": False,
            "node.dont-fallback": True,
            "node.dont-move": True,
        }),
        str(path),
    ]


def progress_wav():
    """Short local cue played by the interaction-owned async player."""
    import io
    rate, duration = 16000, 0.12
    count = int(rate * duration)
    samples = b"".join(struct.pack("<h", int(2200 * math.sin(2 * math.pi * 660 * i / rate)
                      * math.sin(math.pi * i / count) ** 2)) for i in range(count))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples)
    return buffer.getvalue()
