"""
Wyoming event handler — buffers audio chunks then calls HailoWhisperCore.
"""
import asyncio
import json
import logging
import os
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler

from .core import HailoWhisperCore

_LOGGER = logging.getLogger(__name__)

_RAW_LOG_PATH: str | None = os.environ.get("WHISPER_RAW_LOG")
_HA_URL: str = os.environ.get("HA_URL", "http://host.docker.internal:8123")
_HA_TOKEN: str | None = os.environ.get("HA_TOKEN")


def _make_info(model_name: str) -> Info:
    """Build the Wyoming Info descriptor for the loaded model."""
    # Derive a human-readable label from the model name (e.g. "small.en" → "Whisper small (English)")
    size = model_name.split(".")[0].capitalize()
    lang_suffix = " (English)" if model_name.endswith(".en") else ""
    return Info(
        asr=[
            AsrProgram(
                name="wyoming-hailo-whisper",
                description="Whisper STT accelerated by Hailo NPU",
                attribution=Attribution(name="hailo-ai", url="https://hailo.ai"),
                installed=True,
                version="1.0.0",
                models=[
                    AsrModel(
                        name=model_name,
                        description=f"Whisper {size}{lang_suffix}, encoder on Hailo NPU",
                        attribution=Attribution(name="OpenAI", url="https://openai.com"),
                        installed=True,
                        languages=["en"] if model_name.endswith(".en") else [],
                        version="1.0.0",
                    )
                ],
            )
        ]
    )


def _write_log_line(raw: str, corrected: str, response_type: str, detail: str) -> None:
    if not _RAW_LOG_PATH:
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts}\t{raw}\t{corrected}\t{response_type}\t{detail}\n"
    try:
        with open(_RAW_LOG_PATH, "a") as f:
            f.write(line)
    except OSError as e:
        _LOGGER.warning("Could not write to WHISPER_RAW_LOG %s: %s", _RAW_LOG_PATH, e)


def _call_ha_conversation(text: str) -> tuple[str, str]:
    """Call HA /api/conversation/process synchronously. Returns (response_type, detail)."""
    if not _HA_TOKEN:
        return "unknown", "HA_TOKEN not set"
    payload = json.dumps({"text": text, "language": "en"}).encode()
    req = urllib.request.Request(
        f"{_HA_URL}/api/conversation/process",
        data=payload,
        headers={
            "Authorization": f"Bearer {_HA_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        response = body.get("response", {})
        response_type = response.get("response_type", "unknown")
        data = response.get("data", {})
        if response_type == "action_done":
            targets = [t["name"] for t in data.get("success", [])]
            failed = [t["name"] for t in data.get("failed", [])]
            detail = f"ok:{','.join(targets)}" + (f" failed:{','.join(failed)}" if failed else "")
        else:
            detail = data.get("code", "unknown")
        return response_type, detail
    except Exception as e:
        return "error", str(e)


async def _log_with_ha_result(raw: str, corrected: str) -> None:
    """Background task: call HA to validate the corrected transcript, then write log."""
    loop = asyncio.get_event_loop()
    response_type, detail = await loop.run_in_executor(None, _call_ha_conversation, corrected)
    _LOGGER.info("HA result for %r: %s %s", corrected, response_type, detail)
    _write_log_line(raw, corrected, response_type, detail)


class HailoWhisperEventHandler(AsyncEventHandler):
    """
    Handles one Wyoming STT session per connection.
    Collects audio chunks then transcribes with Hailo on AudioStop.
    """

    def __init__(self, *args, core: HailoWhisperCore, **kwargs):
        super().__init__(*args, **kwargs)
        self.core = core
        self._info = _make_info(core.model_name)
        self._audio_buffer: Optional[bytearray] = None
        self._in_utterance: bool = False

    async def handle_event(self, event: Event) -> bool:
        # ── Info request ──────────────────────────────────────────────────────
        if Describe.is_type(event.type):
            await self.write_event(self._info.event())
            return True

        # ── Audio start ───────────────────────────────────────────────────────
        if AudioStart.is_type(event.type):
            self._audio_buffer = bytearray()
            self._in_utterance = True
            _LOGGER.debug("AudioStart received — buffering audio")
            return True

        # ── Audio chunk ───────────────────────────────────────────────────────
        if AudioChunk.is_type(event.type) and self._in_utterance:
            chunk = AudioChunk.from_event(event)
            self._audio_buffer.extend(chunk.audio)
            return True

        # ── Audio stop — run inference ────────────────────────────────────────
        if AudioStop.is_type(event.type) and self._in_utterance:
            self._in_utterance = False
            audio_bytes = bytes(self._audio_buffer)
            self._audio_buffer = None

            _LOGGER.info(
                "AudioStop received — transcribing %d bytes (%.1f s)",
                len(audio_bytes),
                len(audio_bytes) / (16000 * 2),  # 16kHz PCM-16
            )

            try:
                raw, corrected = await self._transcribe_async(audio_bytes)
            except Exception:
                _LOGGER.exception("Transcription failed")
                raw, corrected = "", ""

            _LOGGER.info("Transcript: %r", corrected)
            await self.write_event(Transcript(text=corrected).event())

            if raw or corrected:
                asyncio.create_task(_log_with_ha_result(raw, corrected))

            return True

        # ── Transcribe request (alternative trigger) ──────────────────────────
        if Transcribe.is_type(event.type):
            _LOGGER.debug("Transcribe event received (ignored — using audio stream)")
            return True

        return True

    async def _transcribe_async(self, audio_bytes: bytes) -> tuple[str, str]:
        """Run transcription in a thread so the event loop stays unblocked."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.core.transcribe, audio_bytes)
