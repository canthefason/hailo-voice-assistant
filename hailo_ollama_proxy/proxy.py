#!/usr/bin/env python3
"""
Thin OpenAI-compatibility proxy in front of hailo-ollama.

hailo-ollama only implements the native Ollama API (/api/tags, /api/generate,
/api/chat) and does not support the OpenAI-compatible /v1/* endpoints that
Home Assistant's extended_openai_conversation integration requires.

This proxy:
  - Listens on port 11434 (what Home Assistant talks to)
  - Forwards to hailo-ollama on internal port 11436
  - Translates GET /v1/models → /api/tags (OpenAI list-models format)
  - Fixes two classes of JSON bugs that crash hailo-ollama at runtime

Two-layer JSON fix
------------------
Layer 1 — fix_json_control_chars:
    Home Assistant sends system prompts with literal U+000A (LF) inside JSON
    string values, making the HTTP body invalid JSON (RFC 7159). This layer
    re-encodes those control characters as \\n, \\r, \\t escape sequences.

Layer 2 — sanitize_for_hailo (marker: hailo-sanitize-v1):
    hailo-ollama's HailoRT genai library internally re-serialises message
    content to JSON without escaping newlines — even when the HTTP body was
    already valid. Any newlines still present inside decoded string *values*
    will crash it. This layer parses the (now-valid) JSON and replaces
    newlines/CR/TAB inside messages[].content, prompt, and system fields
    with spaces before forwarding.

Latency optimisation — inject_defaults (marker: hailo-latency-v4):
    Caps max_tokens / num_predict and constrains num_ctx to avoid runaway
    generation on the Hailo-10H NPU. Only injects values the caller omitted.
"""
import http.server
import json
import os
import sys
import urllib.error
import urllib.request

BACKEND_PORT = int(os.environ.get('HAILO_INTERNAL_PORT', '11436'))
LISTEN_PORT  = int(os.environ.get('OLLAMA_PROXY_PORT', '11434'))
BACKEND = 'http://127.0.0.1:' + str(BACKEND_PORT)


def fix_json_control_chars(body_bytes):
    """Escape literal control characters inside JSON string values.

    hailo-ollama strictly rejects U+000A/U+000D inside JSON strings (RFC 7159).
    Home Assistant system prompts contain multi-line text that arrives with
    literal newlines in the HTTP body.
    """
    try:
        body_str = body_bytes.decode('utf-8')
    except Exception:
        return body_bytes

    result = []
    in_string = False
    skip_next = False
    for ch in body_str:
        if skip_next:
            result.append(ch)
            skip_next = False
        elif ch == '\\' and in_string:
            result.append(ch)
            skip_next = True
        elif ch == '"':
            in_string = not in_string
            result.append(ch)
        elif in_string and ch == '\n':
            result.append('\\n')
        elif in_string and ch == '\r':
            result.append('\\r')
        elif in_string and ch == '\t':
            result.append('\\t')
        elif in_string and ord(ch) < 0x20:
            result.append('\\u{:04x}'.format(ord(ch)))
        else:
            result.append(ch)
    return ''.join(result).encode('utf-8')


def sanitize_for_hailo(body_bytes):
    """hailo-sanitize-v1

    HailoRT's prompt renderer re-serialises message content to JSON internally
    without escaping newline characters, causing parse_error.101 at runtime.
    Layer-1 fix (fix_json_control_chars) makes the HTTP body valid JSON, but
    hailo-ollama then takes the decoded string values and re-encodes them to
    JSON again without escaping, so any newlines in the *values* still crash it.
    This function parses the (now-valid) JSON, replaces newlines/CR/TAB chars
    inside messages[].content, prompt, and system fields with spaces, then
    re-serialises — so hailo-ollama never sees control chars in string values.
    """
    try:
        data = json.loads(body_bytes.decode('utf-8'))
    except Exception:
        return body_bytes

    def clean(s):
        if not isinstance(s, str):
            return s
        return s.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')

    changed = False
    for key in ('prompt', 'system'):
        if key in data and isinstance(data[key], str):
            data[key] = clean(data[key])
            changed = True
    for msg in data.get('messages', []):
        if isinstance(msg, dict) and 'content' in msg:
            msg['content'] = clean(msg['content'])
            changed = True

    if not changed:
        return body_bytes
    return json.dumps(data).encode('utf-8')


def inject_defaults(body_bytes, path):
    """hailo-latency-v4

    Inject conservative inference defaults to reduce latency for short
    Home Assistant voice prompts. Only sets values the caller did not specify:
      - max_tokens=120 for /v1/chat/completions  (limits output length)
      - num_predict=60 for /api/generate|chat    (Ollama-native equivalent)
      - num_ctx=512    for /api/generate|chat    (smaller KV cache = faster prefill)

    max_tokens=120 covers a full tool_call JSON (~50 tokens) plus a short
    spoken reply. At ~14 tok/s on Hailo-10H this caps worst-case generation
    at ~8s. 512 ctx covers the HA system prompt + user turn with room to spare.
    """
    try:
        data = json.loads(body_bytes.decode('utf-8'))
    except Exception:
        return body_bytes

    changed = False
    p = path.split('?')[0].rstrip('/')

    if p == '/v1/chat/completions':
        # Always enforce a hard cap — HA sends max_tokens=1022 by default which
        # at ~14 tok/s on Hailo-10H would allow up to 73s of generation.
        if data.get('max_tokens', 0) > 120:
            data['max_tokens'] = 120
            changed = True
        elif 'max_tokens' not in data:
            data['max_tokens'] = 120
            changed = True
    elif p in ('/api/generate', '/api/chat'):
        opts = data.setdefault('options', {})
        if 'num_predict' not in opts:
            opts['num_predict'] = 60
            changed = True
        if 'num_ctx' not in opts:
            opts['num_ctx'] = 512
            changed = True

    if not changed:
        return body_bytes
    return json.dumps(data).encode('utf-8')


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress per-request noise; errors still go to stderr

    def do_GET(self):
        if self.path.rstrip('/') == '/v1/models':
            try:
                r = urllib.request.urlopen(BACKEND + '/api/tags', timeout=10)
                data = json.loads(r.read())
                models = [
                    {'id': m['name'], 'object': 'model', 'created': 0, 'owned_by': 'hailo'}
                    for m in data.get('models', [])
                ]
                body = json.dumps({'object': 'list', 'data': models}).encode()
                self._send(200, body, 'application/json')
            except Exception as exc:
                sys.stderr.write('[proxy] /v1/models error: ' + str(exc) + '\n')
                body = json.dumps({'object': 'list', 'data': []}).encode()
                self._send(200, body, 'application/json')
        else:
            self._forward()

    def do_POST(self):   self._forward()
    def do_DELETE(self): self._forward()
    def do_PUT(self):    self._forward()
    def do_HEAD(self):   self._forward()

    def _forward(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length) if length > 0 else None
        if body and self.headers.get('Content-Type', '').startswith('application/json'):
            body = fix_json_control_chars(body)
            body = sanitize_for_hailo(body)
            body = inject_defaults(body, self.path)
        req = urllib.request.Request(
            BACKEND + self.path, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in ('host', 'content-length', 'transfer-encoding'):
                req.add_header(k, v)
        try:
            r = urllib.request.urlopen(req, timeout=300)
            resp_body = r.read()
            self._send(r.status, resp_body,
                       r.headers.get('Content-Type', 'application/octet-stream'))
        except urllib.error.HTTPError as exc:
            resp_body = exc.read()
            self._send(exc.code, resp_body,
                       exc.headers.get('Content-Type', 'application/json'))
        except Exception as exc:
            sys.stderr.write('[proxy] forward error: ' + str(exc) + '\n')
            self._send(502, b'Bad Gateway', 'text/plain')

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    sys.stderr.write(
        '[proxy] listening on :' + str(LISTEN_PORT) +
        ' -> hailo-ollama:' + str(BACKEND_PORT) + '\n'
    )
    http.server.HTTPServer(('0.0.0.0', LISTEN_PORT), Handler).serve_forever()
