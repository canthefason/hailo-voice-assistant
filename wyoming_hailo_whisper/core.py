"""
HailoWhisperCore — encoder on Hailo NPU, decoder on CPU.

Architecture:
  audio (PCM 16kHz) → mel spectrogram (CPU/numpy)
                    → encoder (Hailo NPU via hailo_platform)
                    → decoder (CPU via openai-whisper internals)
                    → text

Performance note:
  The VDevice, infer_model, and configured_model are created ONCE in __init__
  and reused for every transcription. The previous pattern of opening a new
  VDevice per call incurred NPU setup overhead (kernel calls, HEF programming,
  buffer allocation) on every utterance — the dominant latency source.
"""
import contextlib
import logging
import numpy as np
import torch
import whisper
from pathlib import Path

from hailo_platform import (
    HEF,
    VDevice,
    HailoSchedulingAlgorithm,
)

_LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
N_MELS = 80
CHUNK_LENGTH = 30  # seconds — Whisper encoder HEF is compiled for exactly 30 s (3000 mel frames)


class HailoWhisperCore:
    """
    Manages the Hailo VDevice and Whisper decoder.

    The NPU context (VDevice + configured_model) is opened once at init and
    held open for the lifetime of the object. Each transcribe() call creates
    lightweight per-inference bindings on top of the persistent context.
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

        # Load HEF metadata (does NOT open the device yet)
        self.hef = HEF(self.hef_path)
        _LOGGER.info("HEF loaded: %s", self.hef_path)

        # Load the Whisper model (CPU decoder only)
        _LOGGER.info("Loading Whisper '%s' model (CPU decoder)...", model_name)
        self.whisper_model = whisper.load_model(model_name, device="cpu")
        self.whisper_model.eval()
        _LOGGER.info("Whisper decoder ready.")

        self._decode_options = whisper.DecodingOptions(
            language=language,
            fp16=False,
            without_timestamps=True,
        )

        # ── Open Hailo VDevice ONCE and keep it alive ────────────────────────
        # Re-creating the VDevice on every transcription was the main latency
        # culprit: it involves kernel syscalls, HEF reprogramming into the NPU,
        # and DMA buffer allocation — all repeated needlessly per utterance.
        # With HAILO_VDEVICE_GROUP_ID=SHARED (set in compose.yaml), hailo-ollama
        # can still share the same physical device concurrently.
        _LOGGER.info("Opening Hailo VDevice (persistent)...")
        params = VDevice.create_params()
        # HailoRT 5.x InferModel API requires ROUND_ROBIN scheduling.
        # NONE was valid with the 4.x streaming API but raises HAILO_INVALID_ARGUMENT
        # when used with create_infer_model() / configure().
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN

        # ExitStack owns both context managers so close() releases them in order.
        self._exit_stack = contextlib.ExitStack()
        self._vdevice = self._exit_stack.enter_context(VDevice(params))

        infer_model = self._vdevice.create_infer_model(self.hef_path)
        infer_model.set_batch_size(1)
        self._configured_model = self._exit_stack.enter_context(infer_model.configure())

        # Cache layer names — fixed for the lifetime of this HEF.
        self._input_name  = list(infer_model.input_vstream_params.keys())[0]
        self._output_name = list(infer_model.output_vstream_params.keys())[0]
        _LOGGER.info(
            "Hailo VDevice ready. input=%s output=%s",
            self._input_name, self._output_name,
        )

    # ── Resource management ───────────────────────────────────────────────────

    def close(self) -> None:
        """Explicitly release the NPU VDevice and configured model."""
        self._exit_stack.close()

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
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        if audio.size == 0:
            return ""

        # Pad/trim to exactly 30 s — the encoder HEF requires 3000 mel frames.
        audio = whisper.pad_or_trim(audio, SAMPLE_RATE * CHUNK_LENGTH)

        # Mel spectrogram on CPU (shape: [N_MELS, T])
        mel = whisper.log_mel_spectrogram(audio).numpy()   # (80, 3000)
        mel_batch = mel[np.newaxis].astype(np.float32)     # (1, 80, 3000)

        # Encoder on Hailo NPU (reuses persistent VDevice context)
        encoder_output = self._run_encoder(mel_batch)      # (1, T', D)

        # Decoder on CPU
        encoder_tensor = torch.from_numpy(encoder_output)
        result = whisper.decode(self.whisper_model, encoder_tensor, self._decode_options)
        text = result.text.strip()

        _LOGGER.debug("Transcript: %r", text)
        return text

    # ── Private helpers ───────────────────────────────────────────────────────

    def _run_encoder(self, mel: np.ndarray) -> np.ndarray:
        """
        Run the Whisper encoder on the persistent Hailo NPU context.
        mel: float32 array of shape (1, 80, 3000)
        Returns encoder output as float32 numpy array.

        Only creates lightweight per-call bindings — no device setup overhead.
        """
        bindings = self._configured_model.create_bindings()
        bindings.input(self._input_name).set_buffer(mel)
        self._configured_model.run([bindings], timeout_ms=5000)
        output = np.array(bindings.output(self._output_name).get_buffer())

        _LOGGER.debug(
            "Hailo encoder output shape=%s dtype=%s", output.shape, output.dtype
        )
        return output.astype(np.float32)
