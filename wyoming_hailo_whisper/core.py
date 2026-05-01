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
import logging
import numpy as np
from pathlib import Path

from hailo_platform import VDevice
from hailo_platform.genai import Speech2Text, Speech2TextTask

_LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
SHARED_VDEVICE_GROUP_ID = "SHARED"


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
    ):
        self.hef_path = str(hef_path)
        self.model_name = model_name
        self.language = language
        self.device_id = device_id

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

    def transcribe(self, audio_bytes: bytes) -> str:
        """
        Transcribe raw PCM-16 (16 kHz, mono) bytes to text.
        Returns stripped transcript string.
        """
        if not audio_bytes:
            return ""

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

        text = "".join(seg.text for seg in segments).strip()
        _LOGGER.debug("Transcript: %r", text)
        return text
