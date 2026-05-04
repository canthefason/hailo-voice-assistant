#!/usr/bin/env python3
"""
wyoming-hailo-whisper — Wyoming STT server
  Encoder: Hailo NPU (whisper_small_en_encoder.hef)
  Decoder: CPU via openai-whisper
  Protocol: Wyoming (port 10300)

Usage:
  python3 -m wyoming_hailo_whisper \
    --hef /opt/whisper/encoder.hef \
    --model small.en \
    --uri tcp://0.0.0.0:10300
"""
import argparse
import asyncio
import logging
import sys
from functools import partial

from wyoming.server import AsyncServer

from .handler import HailoWhisperEventHandler
from .core import HailoWhisperCore

_LOGGER = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Wyoming STT server with Hailo NPU")
    parser.add_argument(
        "--hef",
        required=True,
        help="Path to Whisper encoder HEF file (runs on Hailo NPU)",
    )
    parser.add_argument(
        "--model",
        default="small.en",
        help="Whisper model name for decoder (default: small.en)",
    )
    parser.add_argument(
        "--uri",
        default="tcp://0.0.0.0:10300",
        help="Wyoming server URI (default: tcp://0.0.0.0:10300)",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Transcription language (default: en)",
    )
    parser.add_argument(
        "--device-id",
        default=0,
        type=int,
        help="Hailo device index (default: 0)",
    )
    parser.add_argument(
        "--corrections-file",
        default=None,
        help="Path to JSON corrections file (array of [pattern, replacement] pairs). "
             "If omitted, built-in corrections are used.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    _LOGGER.info("Loading Hailo Whisper core (HEF=%s, model=%s)...", args.hef, args.model)
    core = HailoWhisperCore(
        hef_path=args.hef,
        model_name=args.model,
        language=args.language,
        device_id=args.device_id,
        corrections_file=args.corrections_file,
    )
    _LOGGER.info("Hailo Whisper core ready.")

    server = AsyncServer.from_uri(args.uri)
    _LOGGER.info("Wyoming STT server listening on %s", args.uri)

    asyncio.run(
        server.run(partial(HailoWhisperEventHandler, core=core))
    )


if __name__ == "__main__":
    main()
