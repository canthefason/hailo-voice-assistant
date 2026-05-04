"""Unit tests for core.py and handler.py.

Both modules depend on hailo_platform and wyoming, which are only available
inside the Docker container on the Pi.  We stub them in sys.modules before
importing so all tests can run locally (or in CI) without the NPU hardware.
"""

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

# ── Stub hailo_platform before importing core ──────────────────────────────────

_mock_vdevice_cls = MagicMock()
_mock_speech2text_cls = MagicMock()
_mock_speech2text_task = SimpleNamespace(TRANSCRIBE=0)

sys.modules.setdefault("hailo_platform", SimpleNamespace(VDevice=_mock_vdevice_cls))
sys.modules.setdefault(
    "hailo_platform.genai",
    SimpleNamespace(
        Speech2Text=_mock_speech2text_cls,
        Speech2TextTask=_mock_speech2text_task,
    ),
)

# ── Stub wyoming.* before importing handler ────────────────────────────────────

def _wyoming_is_type_factory(name: str):
    """Return an is_type() that matches events by their 'type' string."""
    def is_type(event_type: str) -> bool:
        return event_type == name
    return staticmethod(is_type)

_mock_async_event_handler = MagicMock()
_mock_async_event_handler.__init_subclass__ = classmethod(lambda cls, **kw: None)

# Build minimal wyoming stubs so handler.py imports cleanly.
for _mod, _attrs in [
    ("wyoming.asr",   {"Transcribe": MagicMock(is_type=_wyoming_is_type_factory("Transcribe")),
                       "Transcript": MagicMock(side_effect=lambda text: SimpleNamespace(
                           event=lambda: SimpleNamespace(type="Transcript")))}),
    ("wyoming.audio", {"AudioChunk": MagicMock(is_type=_wyoming_is_type_factory("AudioChunk"),
                                                from_event=MagicMock()),
                       "AudioStart": MagicMock(is_type=_wyoming_is_type_factory("AudioStart")),
                       "AudioStop":  MagicMock(is_type=_wyoming_is_type_factory("AudioStop"))}),
    ("wyoming.event", {"Event": MagicMock()}),
    ("wyoming.info",  {"AsrModel": MagicMock(), "AsrProgram": MagicMock(),
                       "Attribution": MagicMock(), "Describe": MagicMock(
                           is_type=_wyoming_is_type_factory("Describe")),
                       "Info": MagicMock(side_effect=lambda **kw: SimpleNamespace(
                           event=lambda: SimpleNamespace(type="Info")))}),
    ("wyoming.server", {"AsyncEventHandler": type("AsyncEventHandler", (), {
        "__init__": lambda self, *a, **kw: None,
        "write_event": MagicMock(),
    })}),
]:
    sys.modules.setdefault(_mod, SimpleNamespace(**_attrs))

# ── Import the modules under test ─────────────────────────────────────────────

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib
import wyoming_hailo_whisper.core as core
import wyoming_hailo_whisper.handler as handler


# =============================================================================
# core._apply_corrections
# =============================================================================

class TestApplyCorrections(unittest.TestCase):

    def _c(self, text: str) -> str:
        return core._apply_corrections(text, core._CORRECTIONS)

    # living room variants
    def test_leaving_room(self):
        self.assertEqual(self._c("turn on the leaving room lights"), "turn on the living room lights")

    def test_living_rome(self):
        self.assertEqual(self._c("living rome lights off"), "living room lights off")

    def test_living_rum(self):
        self.assertEqual(self._c("living rum is dark"), "living room is dark")

    def test_living_room_unchanged(self):
        self.assertEqual(self._c("living room lights"), "living room lights")

    # turn off variants
    def test_turn_of(self):
        self.assertEqual(self._c("turn of the lights"), "turn off the lights")

    def test_turn_of_end_of_string(self):
        self.assertEqual(self._c("please turn of"), "please turn off")

    def test_turn_off_unchanged(self):
        # "turn off" must not become "turn offf"
        self.assertEqual(self._c("turn off the fan"), "turn off the fan")

    def test_turn_of_followed_by_f_unchanged(self):
        # "turn off" written as two tokens should not double-correct
        self.assertEqual(self._c("turn off"), "turn off")

    # case insensitivity
    def test_case_insensitive_leaving(self):
        self.assertEqual(self._c("LEAVING ROOM"), "living room")

    def test_case_insensitive_turn_of(self):
        self.assertEqual(self._c("TURN OF the lights"), "turn off the lights")

    # multiple corrections in one string
    def test_multiple_corrections(self):
        result = self._c("turn of the leaving room lights")
        self.assertEqual(result, "turn off the living room lights")

    # no-op on unrelated text
    def test_no_match(self):
        text = "set the thermostat to 72 degrees"
        self.assertEqual(self._c(text), text)

    # word-boundary: "leaving" inside a word must not match
    def test_no_partial_match(self):
        self.assertEqual(self._c("leavingroom"), "leavingroom")


# =============================================================================
# core.HailoWhisperCore.transcribe  (NPU fully mocked)
# =============================================================================

class TestHailoWhisperCoreTranscribe(unittest.TestCase):

    def _make_core(self, segments):
        """Build a HailoWhisperCore with mocked NPU internals."""
        with patch("wyoming_hailo_whisper.core.Path") as mock_path:
            mock_path.return_value.is_file.return_value = True
            c = core.HailoWhisperCore.__new__(core.HailoWhisperCore)
            c.hef_path = "/fake/model.hef"
            c.model_name = "small.en"
            c.language = "en"
            c.device_id = 0
            c._corrections = core._CORRECTIONS
            mock_s2t = MagicMock()
            mock_s2t.generate_all_segments.return_value = segments
            c._speech2text = mock_s2t
            c._vdevice = MagicMock()
        return c

    def test_returns_raw_and_corrected_tuple(self):
        seg = SimpleNamespace(text=" leaving room lights on ")
        c = self._make_core([seg])
        raw, corrected = c.transcribe(b"\x00" * 1000)
        self.assertEqual(raw, "leaving room lights on")
        self.assertEqual(corrected, "living room lights on")

    def test_no_correction_needed(self):
        seg = SimpleNamespace(text=" living room lights on ")
        c = self._make_core([seg])
        raw, corrected = c.transcribe(b"\x00" * 1000)
        self.assertEqual(raw, corrected)

    def test_empty_audio_returns_empty_strings(self):
        c = self._make_core([])
        raw, corrected = c.transcribe(b"")
        self.assertEqual(raw, "")
        self.assertEqual(corrected, "")

    def test_multiple_segments_joined(self):
        segs = [SimpleNamespace(text="turn "), SimpleNamespace(text="of the fan")]
        c = self._make_core(segs)
        raw, corrected = c.transcribe(b"\x00" * 1000)
        self.assertEqual(raw, "turn of the fan")
        self.assertEqual(corrected, "turn off the fan")


# =============================================================================
# handler._write_log_line
# =============================================================================

class TestWriteLogLine(unittest.TestCase):

    def test_writes_tab_separated_line(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False) as f:
            path = f.name
        try:
            with patch.object(handler, "_RAW_LOG_PATH", path):
                handler._write_log_line("leaving room", "living room", "action_done", "ok:Living Room")
            with open(path) as f:
                line = f.readline()
            parts = line.rstrip("\n").split("\t")
            self.assertEqual(len(parts), 5)
            self.assertRegex(parts[0], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertEqual(parts[1], "leaving room")
            self.assertEqual(parts[2], "living room")
            self.assertEqual(parts[3], "action_done")
            self.assertEqual(parts[4], "ok:Living Room")
        finally:
            os.unlink(path)

    def test_appends_multiple_lines(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False) as f:
            path = f.name
        try:
            with patch.object(handler, "_RAW_LOG_PATH", path):
                handler._write_log_line("a", "a", "action_done", "ok:X")
                handler._write_log_line("b", "b", "error", "no_valid_targets")
            with open(path) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)
        finally:
            os.unlink(path)

    def test_no_op_when_path_is_none(self):
        with patch.object(handler, "_RAW_LOG_PATH", None):
            # Should not raise even with no file
            handler._write_log_line("x", "x", "action_done", "ok:X")

    def test_oserror_logged_not_raised(self):
        with patch.object(handler, "_RAW_LOG_PATH", "/no/such/dir/file.log"):
            # Should log a warning but not propagate the OSError
            handler._write_log_line("x", "x", "action_done", "ok:X")


# =============================================================================
# handler._call_ha_conversation
# =============================================================================

def _ha_response(response_type: str, data: dict) -> bytes:
    return json.dumps({
        "response": {"response_type": response_type, "data": data},
        "conversation_id": "test-id",
    }).encode()


class TestCallHaConversation(unittest.TestCase):

    def _call(self, text: str, response_bytes: bytes, token: str = "tok"):
        with patch.object(handler, "_HA_TOKEN", token), \
             patch("wyoming_hailo_whisper.handler.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = response_bytes
            return handler._call_ha_conversation(text)

    def test_action_done_single_target(self):
        resp = _ha_response("action_done", {"success": [{"name": "Living Room", "type": "area"}], "failed": []})
        rt, detail = self._call("turn on living room lights", resp)
        self.assertEqual(rt, "action_done")
        self.assertIn("Living Room", detail)
        self.assertTrue(detail.startswith("ok:"))

    def test_action_done_with_failed_targets(self):
        resp = _ha_response("action_done", {
            "success": [{"name": "Living Room", "type": "area"}],
            "failed":  [{"name": "Bedroom", "type": "area"}],
        })
        rt, detail = self._call("turn on lights", resp)
        self.assertEqual(rt, "action_done")
        self.assertIn("failed:Bedroom", detail)

    def test_error_no_valid_targets(self):
        resp = _ha_response("error", {"code": "no_valid_targets"})
        rt, detail = self._call("turn on leaving room lights", resp)
        self.assertEqual(rt, "error")
        self.assertEqual(detail, "no_valid_targets")

    def test_no_token_returns_unknown(self):
        rt, detail = self._call("anything", b"", token=None)
        self.assertEqual(rt, "unknown")
        self.assertIn("HA_TOKEN", detail)

    def test_network_error_returns_error(self):
        import urllib.error
        with patch.object(handler, "_HA_TOKEN", "tok"), \
             patch("wyoming_hailo_whisper.handler.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("connection refused")):
            rt, detail = handler._call_ha_conversation("turn on lights")
        self.assertEqual(rt, "error")
        self.assertIn("connection refused", detail)

    def test_request_sends_correct_payload(self):
        resp = _ha_response("action_done", {"success": [], "failed": []})
        with patch.object(handler, "_HA_TOKEN", "mytoken"), \
             patch.object(handler, "_HA_URL", "http://ha:8123"), \
             patch("wyoming_hailo_whisper.handler.urllib.request.urlopen") as mock_open, \
             patch("wyoming_hailo_whisper.handler.urllib.request.Request") as mock_req:
            mock_open.return_value.__enter__.return_value.read.return_value = resp
            handler._call_ha_conversation("turn on lights")
        args, kwargs = mock_req.call_args
        self.assertIn("http://ha:8123/api/conversation/process", args[0])
        sent = json.loads(kwargs["data"])
        self.assertEqual(sent["text"], "turn on lights")
        self.assertEqual(sent["language"], "en")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer mytoken")


# =============================================================================
# handler._log_with_ha_result  (async)
# =============================================================================

class TestLogWithHaResult(unittest.IsolatedAsyncioTestCase):

    async def test_calls_ha_and_writes_log(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False) as f:
            path = f.name
        try:
            with patch.object(handler, "_RAW_LOG_PATH", path), \
                 patch.object(handler, "_HA_TOKEN", "tok"), \
                 patch("wyoming_hailo_whisper.handler._call_ha_conversation",
                       return_value=("action_done", "ok:Living Room")) as mock_ha:
                await handler._log_with_ha_result("leaving room lights on", "living room lights on")
            mock_ha.assert_called_once_with("living room lights on")
            with open(path) as f:
                line = f.read()
            self.assertIn("leaving room lights on", line)
            self.assertIn("living room lights on", line)
            self.assertIn("action_done", line)
            self.assertIn("ok:Living Room", line)
        finally:
            os.unlink(path)

    async def test_uses_corrected_text_for_ha_call(self):
        """HA is queried with the corrected transcript, not the raw one."""
        with patch.object(handler, "_RAW_LOG_PATH", None), \
             patch.object(handler, "_HA_TOKEN", "tok"), \
             patch("wyoming_hailo_whisper.handler._call_ha_conversation",
                   return_value=("action_done", "ok:X")) as mock_ha:
            await handler._log_with_ha_result("leaving room", "living room")
        mock_ha.assert_called_once_with("living room")


if __name__ == "__main__":
    unittest.main()
