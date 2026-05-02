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

Latency optimisation — inject_defaults (marker: hailo-latency-v5):
    Caps max_tokens / num_predict and constrains num_ctx to avoid runaway
    generation on the Hailo-10H NPU. Only injects values the caller omitted.
    All thresholds are configurable via CLI args (see --help).

Tool call emulation — inject_tool_prompt / rewrite_tool_response (marker: hailo-tools-v1):
    hailo-ollama does not implement the OpenAI tools/tool_choice parameters.
    On the request side, inject_tool_prompt extracts the tool schemas, injects
    a strict JSON output instruction into the system message, and removes
    tools/tool_choice from the request body so hailo-ollama doesn't reject them.
    On the response side, rewrite_tool_response parses the model's text output
    and rewrites it into the OpenAI tool_calls format that Home Assistant expects,
    handling bare JSON, {"name":…,"arguments":…} objects, func({…}) notation,
    and markdown-fenced code blocks.

CLI args override environment variables, which override built-in defaults.
"""
import argparse
import http.server
import json
import os
import re
import sys
import urllib.error
import urllib.request


def _parse_args():
    p = argparse.ArgumentParser(
        description='OpenAI-compatibility proxy for hailo-ollama',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('--listen-port', type=int,
                   default=int(os.environ.get('OLLAMA_PROXY_PORT', '11434')),
                   help='Port this proxy listens on (env: OLLAMA_PROXY_PORT)')
    p.add_argument('--backend-port', type=int,
                   default=int(os.environ.get('HAILO_INTERNAL_PORT', '11436')),
                   help='hailo-ollama backend port (env: HAILO_INTERNAL_PORT)')
    # Inference caps
    p.add_argument('--max-tokens', type=int, default=120,
                   help='Hard cap on max_tokens for /v1/chat/completions')
    p.add_argument('--num-predict', type=int, default=60,
                   help='Hard cap on num_predict for /api/generate and /api/chat')
    p.add_argument('--num-ctx', type=int, default=1024,
                   help='Context window size injected into all paths')
    # Optional quality knobs — only injected when explicitly set
    p.add_argument('--temperature', type=float, default=None,
                   help='Sampling temperature to inject (e.g. 0.1). '
                        'Omit to leave at model default.')
    p.add_argument('--top-p', type=float, default=None,
                   help='Top-p nucleus sampling to inject (e.g. 0.9). '
                        'Omit to leave at model default.')
    p.add_argument('--log-level',
                   choices=['info', 'debug', 'trace'],
                   default=os.environ.get('PROXY_LOG_LEVEL', 'info').lower(),
                   help='Logging verbosity (env: PROXY_LOG_LEVEL). '
                        'info=startup only, '
                        'debug=log requests/responses (system prompt truncated to 200 chars), '
                        'trace=like debug but full bodies, no truncation.')
    return p.parse_args()


ARGS = _parse_args()
LISTEN_PORT  = ARGS.listen_port
BACKEND_PORT = ARGS.backend_port
BACKEND = 'http://127.0.0.1:' + str(BACKEND_PORT)

_SEP = '─' * 60
_SYSTEM_PROMPT_TRUNCATE = 200

def _debug_log(direction, status_or_method, path, body_bytes):
    """Print a formatted request or response block to stderr.

    direction : 'REQ' or 'RES'
    status_or_method : HTTP method (REQ) or status code (RES)
    path : request path
    body_bytes : raw body (may be None)

    Controlled by --log-level:
      info   no-op
      debug  truncates system message to _SYSTEM_PROMPT_TRUNCATE chars
      trace  full bodies, no truncation
    """
    level = ARGS.log_level
    if level == 'info':
        return
    lines = ['\n[proxy:{}:{}] {} {}'.format(level.upper(), direction, status_or_method, path)]
    if body_bytes:
        try:
            data = json.loads(body_bytes.decode('utf-8'))
            if level == 'debug' and 'messages' in data:
                for msg in data['messages']:
                    if msg.get('role') == 'system' and isinstance(msg.get('content'), str):
                        if len(msg['content']) > _SYSTEM_PROMPT_TRUNCATE:
                            msg['content'] = (
                                msg['content'][:_SYSTEM_PROMPT_TRUNCATE]
                                + ' …[{} chars truncated — use --log-level trace to see full prompt]'
                                  .format(len(msg['content']) - _SYSTEM_PROMPT_TRUNCATE)
                            )
            pretty = json.dumps(data, indent=2)
        except Exception:
            pretty = body_bytes.decode('utf-8', errors='replace')
        lines.append(pretty)
    else:
        lines.append('(no body)')
    lines.append(_SEP)
    sys.stderr.write('\n'.join(lines) + '\n')
    sys.stderr.flush()


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
    """hailo-latency-v5

    Inject conservative inference defaults to reduce latency for short
    Home Assistant voice prompts. Caps are configurable via CLI args.

    /v1/chat/completions (OpenAI-compat, used by Home Assistant):
      - max_tokens  hard-capped at --max-tokens (default 120)
      - num_ctx     injected as --num-ctx (default 1024); large enough to
                    hold the HA system prompt + entity list + user turn
      - temperature injected if --temperature is set
      - top_p       injected if --top-p is set

    /api/generate, /api/chat (native Ollama API):
      - num_predict hard-capped at --num-predict (default 60)
      - num_ctx     injected into options{} at --num-ctx
      - temperature / top_p injected into options{} when set

    max_tokens=120 covers a full tool_call JSON (~50 tokens) plus a short
    spoken reply. At ~14 tok/s on Hailo-10H this caps worst-case generation
    at ~8s.
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
        if data.get('max_tokens', 0) > ARGS.max_tokens or 'max_tokens' not in data:
            data['max_tokens'] = ARGS.max_tokens
            changed = True
        if 'num_ctx' not in data:
            data['num_ctx'] = ARGS.num_ctx
            changed = True
        if ARGS.temperature is not None and 'temperature' not in data:
            data['temperature'] = ARGS.temperature
            changed = True
        if ARGS.top_p is not None and 'top_p' not in data:
            data['top_p'] = ARGS.top_p
            changed = True

    elif p in ('/api/generate', '/api/chat'):
        opts = data.setdefault('options', {})
        if opts.get('num_predict', 0) > ARGS.num_predict or 'num_predict' not in opts:
            opts['num_predict'] = ARGS.num_predict
            changed = True
        if 'num_ctx' not in opts:
            opts['num_ctx'] = ARGS.num_ctx
            changed = True
        if ARGS.temperature is not None and 'temperature' not in opts:
            opts['temperature'] = ARGS.temperature
            changed = True
        if ARGS.top_p is not None and 'top_p' not in opts:
            opts['top_p'] = ARGS.top_p
            changed = True

    if not changed:
        return body_bytes
    return json.dumps(data).encode('utf-8')


def inject_tool_prompt(body_bytes):
    """hailo-tools-v1 (request side)

    hailo-ollama does not implement the OpenAI tools/tool_choice parameters.
    This layer emulates tool calling via prompt engineering:
      1. Extracts tool schemas from the request
      2. Injects a strict JSON output instruction into the system message
         (no literal newlines — sanitize_for_hailo runs after this)
      3. Removes tools/tool_choice from the request body

    Returns (transformed_body_bytes, had_tools: bool).
    """
    try:
        data = json.loads(body_bytes.decode('utf-8'))
    except Exception:
        return body_bytes, False

    tools = data.get('tools')
    if not tools:
        return body_bytes, False

    # Compact description of all available tools
    tool_desc = ' | '.join(
        '{}: {}'.format(t['function']['name'], t['function'].get('description', ''))
        for t in tools if 'function' in t
    )

    # Concrete example using the execute_services schema HA always sends.
    # Single-line — sanitize_for_hailo will run next and collapse any \n to spaces.
    example = (
        '{"name": "execute_services", "arguments": {"list": ['
        '{"domain": "light", "service": "turn_on", '
        '"service_data": {"entity_id": "light.office_lights", "brightness_pct": 50}}'
        ']}}'
    )

    instruction = (
        ' TOOL CALL RULES: Respond with ONLY a JSON object — no markdown, no explanation, nothing else.'
        ' Use this exact format: {"name": "<tool_name>", "arguments": <arguments object>}.'
        ' Available tools: ' + tool_desc + '.'
        ' Example: ' + example
    )

    # Append to the existing system message, or prepend a new one
    messages = data.get('messages', [])
    injected = False
    for msg in messages:
        if msg.get('role') == 'system':
            msg['content'] = (msg.get('content') or '') + instruction
            injected = True
            break
    if not injected:
        messages.insert(0, {'role': 'system', 'content': instruction.strip()})
        data['messages'] = messages

    # Remove fields hailo-ollama does not understand
    data.pop('tools', None)
    data.pop('tool_choice', None)

    return json.dumps(data).encode('utf-8'), True


def _try_parse_tool_call(text):
    """Extract (fn_name, arguments_dict) from model output, or return None.

    Handles these patterns the model may produce:
      {"name": "fn", "arguments": {...}}    ideal format matching our injection
      {"list": [...]}                        bare execute_services arguments
      fn_name({"list": [...]})               Python-style call notation
      ```json\\n{...}\\n```                  markdown-wrapped JSON
    """
    text = text.strip()

    # Strip markdown fences
    if '```' in text:
        text = re.sub(r'```[a-z]*\n?', '', text).strip()

    # Pattern: func_name({...})
    fn_match = re.match(r'^(\w+)\s*\((\{.+\})\)\s*$', text, re.DOTALL)
    if fn_match:
        try:
            return fn_match.group(1), json.loads(fn_match.group(2))
        except Exception:
            pass

    # Pattern: pure JSON object
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            # {"name": "...", "arguments": {...}}
            if 'name' in parsed and 'arguments' in parsed:
                return parsed['name'], parsed['arguments']
            # {"list": [...]} — bare execute_services arguments
            if 'list' in parsed:
                return 'execute_services', parsed
    except Exception:
        pass

    return None


def rewrite_tool_response(body_bytes):
    """hailo-tools-v1 (response side)

    hailo-ollama returns the model output as plain text in message.content.
    This layer parses that text, and if it looks like a tool call, rewrites
    the response into the OpenAI tool_calls format that Home Assistant expects:
      - message.content → null
      - message.tool_calls → [{id, type, function: {name, arguments}}]
      - finish_reason → "tool_calls"

    If the content cannot be parsed as a tool call it is returned unchanged,
    so HA gets the plain text reply as a fallback.
    """
    try:
        resp = json.loads(body_bytes.decode('utf-8'))
    except Exception:
        return body_bytes

    choices = resp.get('choices', [])
    if not choices:
        return body_bytes

    choice = choices[0]
    message = choice.get('message', {})

    # Already a proper tool_calls response — nothing to do
    if message.get('tool_calls'):
        return body_bytes

    content = message.get('content') or ''
    result = _try_parse_tool_call(content)
    if result is None:
        return body_bytes

    fn_name, arguments = result
    choice['message'] = {
        'role': 'assistant',
        'content': None,
        'tool_calls': [{
            'id': 'call_0',
            'type': 'function',
            'function': {
                'name': fn_name,
                'arguments': json.dumps(arguments),
            }
        }]
    }
    choice['finish_reason'] = 'tool_calls'
    resp['choices'] = [choice]
    return json.dumps(resp).encode('utf-8')


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Suppress default per-request noise; debug logging is handled explicitly.
        if ARGS.log_level != 'info':
            sys.stderr.write('[proxy] ' + (fmt % args) + '\n')

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
        had_tools = False
        if body and self.headers.get('Content-Type', '').startswith('application/json'):
            body = fix_json_control_chars(body)
            body, had_tools = inject_tool_prompt(body)  # extract tools before sanitize
            body = sanitize_for_hailo(body)             # collapse newlines incl. injected text
            body = inject_defaults(body, self.path)
        _debug_log('REQ', self.command, self.path, body)
        req = urllib.request.Request(
            BACKEND + self.path, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in ('host', 'content-length', 'transfer-encoding'):
                req.add_header(k, v)
        try:
            r = urllib.request.urlopen(req, timeout=300)
            resp_body = r.read()
            if had_tools:
                resp_body = rewrite_tool_response(resp_body)
            _debug_log('RES', r.status, self.path, resp_body)
            self._send(r.status, resp_body,
                       r.headers.get('Content-Type', 'application/octet-stream'))
        except urllib.error.HTTPError as exc:
            resp_body = exc.read()
            _debug_log('RES', exc.code, self.path, resp_body)
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
        '[proxy] listening on :{listen} -> hailo-ollama:{backend} | '
        'max_tokens={mt} num_predict={np} num_ctx={nc}'
        '{temp}{topp}{debug}\n'.format(
            listen=LISTEN_PORT,
            backend=BACKEND_PORT,
            mt=ARGS.max_tokens,
            np=ARGS.num_predict,
            nc=ARGS.num_ctx,
            temp=' temperature={}'.format(ARGS.temperature) if ARGS.temperature is not None else '',
            topp=' top_p={}'.format(ARGS.top_p) if ARGS.top_p is not None else '',
            debug='' if ARGS.log_level == 'info' else ' log-level={}'.format(ARGS.log_level),
        )
    )
    http.server.HTTPServer(('0.0.0.0', LISTEN_PORT), Handler).serve_forever()
