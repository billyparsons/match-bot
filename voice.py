"""Voice processing: STT (OpenAI Whisper) and TTS (Inworld)."""

import base64
import json
import logging
import os
import subprocess
import uuid

import requests

from config import CONFIG

log = logging.getLogger("cleo.voice")


# ---------------------------------------------------------------------------
# Key / config loaders
# ---------------------------------------------------------------------------

def _load_openai_key() -> str | None:
    """Load OpenAI API key from workspace private config."""
    path = os.path.join(CONFIG["workspace"], "private", "openai.json")
    try:
        with open(path) as f:
            return json.load(f).get("api_key")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _load_inworld_config() -> dict | None:
    """Load Inworld TTS config from workspace private config.

    Returns dict with 'api_key' and optional 'voice' (defaults to 'Ashley').
    """
    path = os.path.join(CONFIG["workspace"], "private", "inworld.json")
    try:
        with open(path) as f:
            data = json.load(f)
        if not data.get("api_key"):
            return None
        data.setdefault("voice", "Olivia")
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Speech-to-Text (Whisper)
# ---------------------------------------------------------------------------

def transcribe_audio(file_path: str) -> str:
    """Transcribe an audio file using OpenAI Whisper API.

    Returns transcript text, or a bracketed error string on failure.
    """
    api_key = _load_openai_key()
    if not api_key:
        return "[transcription unavailable: no OpenAI API key]"

    if not os.path.exists(file_path):
        return "[transcription unavailable: audio file not found]"

    # Convert raw AAC/ADTS to M4A container if needed (Whisper rejects raw .aac)
    converted_path = None
    upload_path = file_path
    if file_path.endswith(".aac"):
        converted_path = file_path.rsplit(".", 1)[0] + "_converted.m4a"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", file_path, "-c:a", "copy", converted_path],
                capture_output=True,
                check=True,
            )
            upload_path = converted_path
        except subprocess.CalledProcessError as e:
            log.error("AAC→M4A conversion failed: %s", e.stderr.decode(errors="replace")[:200])
            return f"[transcription failed: AAC conversion error]"

    try:
        filename = os.path.basename(upload_path)
        with open(upload_path, "rb") as f:
            resp = requests.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (filename, f)},
                data={"model": "whisper-1"},
                timeout=30,
            )
        resp.raise_for_status()
        text = resp.json().get("text", "").strip()
        if text:
            log.info("Transcribed audio (%s): %s", os.path.basename(file_path), text[:80])
        return text or "[transcription returned empty]"
    except Exception as e:
        log.error("Transcription failed for %s: %s", file_path, e)
        return f"[transcription failed: {e}]"
    finally:
        if converted_path and os.path.exists(converted_path):
            os.unlink(converted_path)


# ---------------------------------------------------------------------------
# Text-to-Speech (Inworld)
# ---------------------------------------------------------------------------

def generate_speech(text: str, voice: str | None = None) -> str:
    """Generate speech audio using Inworld TTS-1.5-Max.

    Returns file path to an OGG/Opus audio file, or an error string.
    """
    config = _load_inworld_config()
    if not config:
        return (
            f"Error: no Inworld API key — create {CONFIG['workspace']}/private/inworld.json "
            'with {"api_key": "...", "voice": "Ashley"}'
        )

    # Verify ffmpeg is available
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "Error: ffmpeg not installed (needed for audio conversion)"

    api_key = config["api_key"]
    voice = voice or config.get("voice", "Ashley")

    try:
        resp = requests.post(
            "https://api.inworld.ai/tts/v1/voice",
            headers={
                "Authorization": f"Basic {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "voiceId": voice,
                "modelId": "inworld-tts-1.5-max",
                "speaking_rate": config.get("speaking_rate", 2.3),
            },
            timeout=30,
        )
        resp.raise_for_status()

        audio_b64 = resp.json().get("audioContent")
        if not audio_b64:
            return "Error: no audio content in Inworld TTS response"

        # Inworld returns mp3 — save raw, then post-process with ffmpeg
        file_id = uuid.uuid4()
        raw_path = f"/tmp/cleo-voice-{file_id}-raw.mp3"
        mp3_path = f"/tmp/cleo-voice-{file_id}.mp3"

        with open(raw_path, "wb") as f:
            f.write(base64.b64decode(audio_b64))

        # Post-process: speed up + pitch down + ethereal chorus + small room reverb
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", raw_path,
                    "-af", "atempo=1.3,asetrate=44100*0.96,aresample=44100,chorus=0.7:0.9:45|55:0.35|0.25:0.3|0.2:2|2.5,aecho=0.8:0.75:15|25:0.12|0.06",
                    mp3_path,
                ],
                capture_output=True, check=True,
            )
            os.unlink(raw_path)
        except subprocess.CalledProcessError as e:
            log.warning("Voice post-processing failed, using raw audio: %s", e.stderr.decode(errors="replace")[:200])
            os.rename(raw_path, mp3_path)

        log.info("Generated voice saved to %s (%d bytes)", mp3_path, os.path.getsize(mp3_path))
        return mp3_path

    except Exception as e:
        log.error("TTS generation failed: %s", e)
        return f"Error: TTS generation failed — {e}"
