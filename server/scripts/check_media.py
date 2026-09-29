"""Read-only service checks plus silent /voice and in-memory /tts smoke tests.

Run from server/: .venv/bin/python scripts/check_media.py
Never submits an Overseerr/Arr request or prints configuration/credentials.
"""
import argparse
import io
from pathlib import Path
import sys
import wave

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integrations.overseerr_client import OverseerrClient
from integrations.radarr_client import RadarrClient
from integrations.sonarr_client import SonarrClient


def check_services():
    for name, client in [("Radarr", RadarrClient()), ("Sonarr", SonarrClient())]:
        status = client.system_status()
        print(f"{name}: connected, version {status['version']}")
    api = OverseerrClient()
    status = api._request("GET", "/status")
    movies = api.search_movies("Interstellar")
    if not movies:
        raise RuntimeError("Overseerr search returned no movies")
    api.get_movie(movies[0].media_id)
    print(f"Overseerr: connected, version {status.get('version', 'unknown')}; search/detail passed")


def check_voice(client):
    health = client.get("/health")
    health.raise_for_status()
    data = health.json()
    if data.get("status") != "ok" or data.get("tts") != "ready":
        raise RuntimeError("Cube health or Kokoro readiness failed")
    print("/health: 200; Kokoro ready")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 16000)
    for path in ["/transcribe", "/voice"]:
        response = client.post(path, files={"file": ("silence.wav", buffer.getvalue(), "audio/wav")})
        response.raise_for_status()
        result = response.json()
        if path == "/voice" and (result.get("type") != "unhandled" or result.get("transcript")):
            raise RuntimeError("Silent voice check unexpectedly produced a command or transcript")
        print(f"{path}: 200; silent audio accepted")
    response = client.post("/tts", json={"text": "Cube media assistant check."})
    response.raise_for_status()
    with wave.open(io.BytesIO(response.content)) as audio:
        if audio.getnframes() <= 0:
            raise RuntimeError("Empty TTS audio")
        print(f"/tts: 200; valid WAV, {audio.getframerate()} Hz")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    try:
        check_services()
        with httpx.Client(base_url=args.base, timeout=60, trust_env=False, follow_redirects=False) as client:
            check_voice(client)
    except Exception as error:
        print(f"Media validation failed ({type(error).__name__}); no credentials or upstream body displayed.")
        sys.exit(1)
