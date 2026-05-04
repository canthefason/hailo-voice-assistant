"""
HailoWhisperCore — full speech-to-text pipeline on Hailo NPU via genai API.

Architecture:
  audio (PCM 16kHz) → hailo_platform.genai.Speech2Text
                        (mel spectrogram + encoder + decoder all on Hailo-10H)
                    → text

The Speech2Text genai API replaces the old low-level VDevice/create_infer_model
path. The HEF distributed with HailoRT 5.x (whisper_small_10s_no_kqs_decoder)
is a combined encoder+decoder model that only works with the genai API; the
InferModel API raises HAILO_INVALID_ARGUMENT on configure() with this HEF.

Performance note:
  VDevice and Speech2Text are created ONCE in __init__ and reused for every
  transcription call. The previous pattern of re-creating the VDevice per call
  incurred NPU setup overhead (kernel calls, HEF programming, buffer allocation)
  on every utterance — the dominant latency source.
"""
import json
import logging
import re
import numpy as np
from pathlib import Path

from hailo_platform import VDevice
from hailo_platform.genai import Speech2Text, Speech2TextTask

_LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
SHARED_VDEVICE_GROUP_ID = "SHARED"

# Built-in fallback corrections used when no corrections file is provided.
# Each tuple is (compiled pattern, replacement). Applied in order after transcription.
_CORRECTIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bleaving room\b", re.IGNORECASE), "living room"),
    (re.compile(r"\bliving rome\b", re.IGNORECASE), "living room"),
    (re.compile(r"\bliving rum\b", re.IGNORECASE), "living room"),
    (re.compile(r"\bturn of\b(?!\s*f)", re.IGNORECASE), "turn off"),
]


def _load_corrections_file(path: str) -> list[tuple[re.Pattern, str]]:
    """Load corrections from a JSON file.

    Expected format: array of [pattern, replacement] pairs.
    All patterns are compiled with IGNORECASE; use inline (?-i) to override.

    Example:
        [
          ["\\\\bleaving room\\\\b", "living room"],
          ["\\\\bturn of\\\\b(?!\\\\s*f)", "turn off"]
        ]
    """
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise ValueError(f"corrections file must be a JSON array, got {type(data).__name__}")
    corrections = []
    for i, entry in enumerate(data):
        if not (isinstance(entry, list) and len(entry) == 2):
            raise ValueError(f"corrections[{i}] must be a [pattern, replacement] pair")
        pattern_str, replacement = entry
        corrections.append((re.compile(pattern_str, re.IGNORECASE), replacement))
    _LOGGER.info("Loaded %d correction(s) from %s", len(corrections), path)
    return corrections


def _apply_corrections(text: str, corrections: list[tuple[re.Pattern, str]]) -> str:
    for pattern, replacement in corrections:
        text = pattern.sub(replacement, text)
    return text


class HailoWhisperCore:
    """
    Manages the Hailo VDevice and Speech2Text genai pipeline.

    The NPU context (VDevice + Speech2Text) is opened once at init and
    held open for the lifetime of the object. Each transcribe() call is
    lightweight — it reuses the persistent NPU context.
    """

    def __init__(
        self,
        hef_path: str,
        model_name: str = "small.en",
        language: str = "en",
        device_id: int = 0,
        corrections_file: str | None = None,
    ):
        self.hef_path = str(hef_path)
        self.model_name = model_name
        self.language = language
        self.device_id = device_id

        if corrections_file is not None:
            self._corrections = _load_corrections_file(corrections_file)
        else:
            self._corrections = _CORRECTIONS

        if not Path(self.hef_path).is_file():
            raise FileNotFoundError(f"HEF not found: {self.hef_path}")

        _LOGGER.info("Opening Hailo VDevice (persistent, group_id=SHARED)...")
        params = VDevice.create_params()
        # group_id="SHARED" enables multi-process device sharing so hailo-whisper
        # and hailo-ollama can concurrently use the same Hailo-10H device.
        params.group_id = SHARED_VDEVICE_GROUP_ID
        self._vdevice = VDevice(params)

        _LOGGER.info("Initialising Speech2Text pipeline from HEF: %s", self.hef_path)
        self._speech2text = Speech2Text(self._vdevice, self.hef_path)
        _LOGGER.info("Hailo Speech2Text pipeline ready.")

    # ── Resource management ───────────────────────────────────────────────────

    def close(self) -> None:
        """Explicitly release the Speech2Text pipeline and VDevice."""
        try:
            self._speech2text.release()
        except Exception:
            pass
        try:
            self._vdevice.release()
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # ── Public API ────────────────────────────────────────────────────────────

    def transcribe(self, audio_bytes: bytes) -> tuple[str, str]:
        """
        Transcribe raw PCM-16 (16 kHz, mono) bytes to text.
        Returns (raw, corrected) where raw is the unmodified Whisper output.
        """
        if not audio_bytes:
            return "", ""

        # Convert PCM-16 → float32 in [-1, 1], little-endian (required by genai API)
        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        audio_f32 = audio_int16.astype(np.float32) / 32768.0
        audio_f32 = audio_f32.astype("<f4")  # ensure little-endian float32

        segments = self._speech2text.generate_all_segments(
            audio_data=audio_f32,
            task=Speech2TextTask.TRANSCRIBE,
            language=self.language,
            timeout_ms=30000,
        )

        raw = "".join(seg.text for seg in segments).strip()
        corrected = _apply_corrections(raw, self._corrections)
        _LOGGER.info("WHISPER_RAW: %s", raw)
        if corrected != raw:
            _LOGGER.info("WHISPER_CORRECTED: %s", corrected)
        return raw, corrected
