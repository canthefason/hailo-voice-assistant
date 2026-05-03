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

Conversation role sanitization — sanitize_conversation_roles:
    After the proxy rewrites a response into tool_calls format, Home Assistant
    stores it in conversation history and echoes it back on the next turn.
    hailo-ollama only supports system/user/assistant roles with non-null string
    content; the OpenAI assistant+tool_calls (content:null) and role:tool messages
    cause a null-pointer crash in oatpp. This layer rewrites those back to plain
    assistant/user messages before forwarding.

Prompt strings — prompts.json (env: PROXY_CONFIG):
    The three strings used by inject_tool_prompt live in prompts.json
    next to this script (or the file given by --config / PROXY_CONFIG):
      "example"               — few-shot examples
      "instruction_template"  — injected into the system message; literal
                                tokens {tool_desc} and {example} are
                                replaced at request time
      "followup_hint"         — appended on tool-result follow-up turns
    Missing or empty values mean "inject nothing" for that slot — there
    are no built-in fallback strings in the code.

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
    p.add_argument('--config', metavar='FILE',
                   default=os.environ.get(
                       'PROXY_CONFIG',
                       os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts.json'),
                   ),
                   help='Path to a JSON config file for prompt strings '
                        '(example, instruction_template, followup_hint). '
                        'Defaults to prompts.json next to this script. '
                        'Missing keys → no injection. (env: PROXY_CONFIG)')
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
    p.add_argument('--no-retry-on-rejection',
                   dest='retry_on_rejection', action='store_false',
                   help='Disable single retry on tool-call validation rejection. '
                        'When enabled (default), if the model emits a JSON-shaped '
                        'tool call that fails validation (empty list, hallucinated '
                        'entity_id, …), the proxy resends the request once with '
                        'temperature=0.7 to perturb sampling. Costs ~one extra '
                        'inference (~10-15s) on failed first attempts only.')
    p.set_defaults(retry_on_rejection=True)
    return p.parse_args()


ARGS = _parse_args()
LISTEN_PORT  = ARGS.listen_port
BACKEND_PORT = ARGS.backend_port
BACKEND = 'http://127.0.0.1:' + str(BACKEND_PORT)

def _load_prompt_config(path):
    """Load prompt strings from a JSON file (prompts.json by default).

    Recognised keys (all optional):
      "example"               — few-shot examples appended via {example} in instruction_template
      "instruction_template"  — injected into system message on tool turns;
                                {tool_desc} and {example} are simple token replacements
      "followup_hint"         — appended to system message on tool-result follow-up turns

    Missing or empty-string values mean "inject nothing" for that slot.
    Unknown keys are ignored.
    """
    cfg = {'example': '', 'instruction_template': '', 'followup_hint': ''}
    if not path:
        return cfg
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        for key in cfg:
            if key in data:
                if not isinstance(data[key], str):
                    sys.stderr.write(
                        '[proxy] config: key {!r} must be a string — ignored\n'.format(key)
                    )
                    continue
                cfg[key] = data[key]
        sys.stderr.write('[proxy] loaded prompt config from {}\n'.format(path))
    except FileNotFoundError:
        sys.stderr.write('[proxy] prompts.json not found at {!r} — no prompt injection\n'
                         .format(path))
    except Exception as exc:
        sys.stderr.write('[proxy] failed to load config {!r}: {} — no prompt injection\n'
                         .format(path, exc))
    return cfg


PROMPT_CONFIG = _load_prompt_config(ARGS.config)

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
        mt = data.get('max_tokens')
        if mt is None:
            # Caller didn't set it — use our default
            data['max_tokens'] = ARGS.max_tokens
            changed = True
        elif mt > 500:
            # Pathologically high (HA default is 1022 — at ~14 tok/s that's 73s).
            # Clamp to our default. Values ≤ 500 are left alone: they were either
            # set intentionally by the caller or raised by inject_tool_prompt.
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


def sanitize_conversation_roles(body_bytes):
    """Convert OpenAI-only message roles that hailo-ollama does not support.

    hailo-ollama (oatpp / HailoRT) only handles system/user/assistant roles
    with non-null string content. After the proxy rewrites a response into
    tool_calls format, Home Assistant stores that in conversation history and
    echoes it back on the next turn, introducing two unsupported constructs:

      assistant + tool_calls + content:null
        → hailo-ollama dereferences content as std::string → null-pointer crash
        → rewritten to: assistant with the tool call serialised as JSON text

      role:"tool" (tool result)
        → unknown role → crash or silent drop
        → rewritten to: user message "Result of <name>: <content>"

    This runs before sanitize_for_hailo so the new string values get their
    newlines collapsed in the same pass.
    """
    try:
        data = json.loads(body_bytes.decode('utf-8'))
    except Exception:
        return body_bytes

    messages = data.get('messages', [])
    changed = False
    new_messages = []

    for msg in messages:
        role = msg.get('role')

        if role == 'assistant' and msg.get('tool_calls') and not msg.get('content'):
            # Serialise each tool call back to a JSON string so the model has
            # context about what it previously called.
            parts = []
            for tc in msg['tool_calls']:
                fn = tc.get('function', {})
                try:
                    args = json.loads(fn.get('arguments', '{}'))
                except Exception:
                    args = fn.get('arguments', '')
                parts.append(json.dumps({'name': fn.get('name', ''), 'arguments': args}))
            new_messages.append({'role': 'assistant', 'content': ' '.join(parts)})
            changed = True

        elif role == 'tool':
            # Fold the tool result into a user turn so the model sees the outcome.
            name = msg.get('name', 'tool')
            content = msg.get('content', '')
            new_messages.append({'role': 'user',
                                  'content': 'Result of {}: {}'.format(name, content)})
            changed = True

        else:
            new_messages.append(msg)

    if not changed:
        return body_bytes

    data['messages'] = new_messages
    return json.dumps(data).encode('utf-8')


def _is_tool_result_followup(messages):
    """True if the request is HA echoing a tool result for natural-language summary.

    HA's flow after a successful tool_call: it appends a {role:"tool", ...}
    message and re-sends with tools[] still present, expecting the model to
    summarise the outcome. Injecting JSON-only instructions on this turn makes
    the model emit another (empty) tool call — which then fails validation and
    leaves raw JSON in the spoken response.
    """
    for msg in messages:
        if msg.get('role') == 'tool':
            return True
    return False


def inject_tool_prompt(body_bytes):
    """hailo-tools-v1 (request side)

    hailo-ollama does not implement the OpenAI tools/tool_choice parameters.
    This layer emulates tool calling via prompt engineering:
      1. Extracts tool schemas from the request
      2. Injects a strict JSON output instruction into the system message
         (no literal newlines — sanitize_for_hailo runs after this)
      3. Removes tools/tool_choice from the request body

    On tool-result follow-up turns we strip tools/tool_choice but skip the
    JSON-only injection — the model should respond in natural language so HA
    has something speakable to read.

    Returns (transformed_body_bytes, had_tools: bool). had_tools controls
    whether rewrite_tool_response runs; we set it False on follow-up turns
    so a natural-language reply is passed through untouched.
    """
    try:
        data = json.loads(body_bytes.decode('utf-8'))
    except Exception:
        return body_bytes, False

    tools = data.get('tools')
    if not tools:
        return body_bytes, False

    messages = data.get('messages', [])
    followup = _is_tool_result_followup(messages)

    # Remove fields hailo-ollama does not understand — always.
    data.pop('tools', None)
    data.pop('tool_choice', None)

    if followup:
        # Tool already executed; ask for a one-sentence natural-language summary.
        # Skipping the example injection avoids the model parroting JSON back.
        # The "only mention the device the user asked about" clause stops the
        # model from listing every entity it saw in the system prompt
        # (qwen2.5:1.5b otherwise hallucinates a status report on unrelated
        # devices). "Do not invent details" stops fabricated brightness values.
        # Even with this prompt the model often runs on past sentence one —
        # the response side truncates to the first sentence as a hard cap.
        followup_hint = PROMPT_CONFIG['followup_hint']
        if followup_hint:
            for msg in messages:
                if msg.get('role') == 'system':
                    msg['content'] = (msg.get('content') or '') + followup_hint
                    break
        # had_tools=False so rewrite_tool_response is skipped, but we still
        # signal via the second return slot that the response should be
        # truncated. Caller treats False as "no tool rewrite" anyway.
        return json.dumps(data).encode('utf-8'), 'followup'

    template = PROMPT_CONFIG['instruction_template']
    if not template:
        return json.dumps(data).encode('utf-8'), True

    # Compact description of all available tools
    tool_desc = ' | '.join(
        '{}: {}'.format(t['function']['name'], t['function'].get('description', ''))
        for t in tools if 'function' in t
    )

    # {tool_desc} and {example} are simple token replacements — no .format() escaping needed.
    instruction = (template
                   .replace('{tool_desc}', tool_desc)
                   .replace('{example}', PROMPT_CONFIG['example']))

    # Append to the existing system message, or prepend a new one
    injected = False
    for msg in messages:
        if msg.get('role') == 'system':
            msg['content'] = (msg.get('content') or '') + instruction
            injected = True
            break
    if not injected:
        messages.insert(0, {'role': 'system', 'content': instruction.strip()})
        data['messages'] = messages

    # Ensure enough headroom for a complete tool call JSON.
    # inject_defaults caps max_tokens at ARGS.max_tokens (default 120) to limit
    # runaway generation, but that floor is too low for a pretty-printed tool call
    # (~150-200 tokens). Override upward here so inject_defaults won't clamp it back.
    min_tool_tokens = max(ARGS.max_tokens, 250)
    if data.get('max_tokens', 0) < min_tool_tokens:
        data['max_tokens'] = min_tool_tokens

    return json.dumps(data).encode('utf-8'), True


def _fix_json(text):
    """Best-effort repair of common LLM JSON output artifacts.

    - Strips markdown fences
    - Strips stray leading/trailing quote characters (e.g. model wraps output in "...")
    - Removes trailing commas before ] or } (e.g. [1, 2,] or {"a":1,})
    - Appends missing closing brackets/braces for truncated output
    """
    if '```' in text:
        text = re.sub(r'```[a-z]*\n?', '', text).strip()
    # Strip stray surrounding quotes the model sometimes adds
    text = text.strip('"\'')
    # Remove trailing commas before closing brackets/braces
    text = re.sub(r',\s*([}\]])', r'\1', text)
    # Balance unmatched opening braces/brackets (handles token-limit truncation)
    stack = []
    in_string = False
    escape = False
    for ch in text:
        if escape:
            escape = False
        elif ch == '\\' and in_string:
            escape = True
        elif ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch in '{[':
                stack.append('}' if ch == '{' else ']')
            elif ch in '}]' and stack and stack[-1] == ch:
                stack.pop()
    text += ''.join(reversed(stack))
    return text


def _merge_duplicate_service_data(text):
    """Merge repeated `"service_data": {...}` blocks inside a single object.

    The model occasionally splits a single service call into two service_data
    keys (e.g. one with entity_id, another with brightness_pct). json.loads
    keeps only the last duplicate, dropping the entity_id and breaking
    validation. Detected by counting nested service_data occurrences inside
    one `{ ... }` and merging their contents before parse.

    Best-effort regex pass — any object that doesn't match cleanly is left
    unchanged for the regular parser to handle.
    """
    pattern = re.compile(
        r'"service_data"\s*:\s*(\{[^{}]*\})\s*,\s*"service_data"\s*:\s*(\{[^{}]*\})'
    )

    def merge(m):
        try:
            a = json.loads(m.group(1))
            b = json.loads(m.group(2))
            a.update(b)
            return '"service_data": ' + json.dumps(a)
        except Exception:
            return m.group(0)

    prev = None
    while prev != text:
        prev = text
        text = pattern.sub(merge, text)
    return text


_ENTITY_ID_RE = re.compile(r'^([a-z_]+\.[a-zA-Z0-9_]+)')

# Loose match for any entity_id token anywhere in a string (used to scrape
# HA's "Available Devices: ..." section in the system prompt).
_ENTITY_ID_TOKEN_RE = re.compile(r'\b([a-z_]+\.[a-zA-Z0-9_]+)\b')


def extract_known_entities(body_bytes):
    """Return the set of entity_ids HA listed in the system prompt, or None.

    HA's Extended OpenAI Conversation integration formats exposed devices as
    `Available Devices: light.x,Friendly,state light.y,Friendly,state ...`
    inside the system message. Regex-scrape `domain.object_id` tokens from
    every message; we use this set to validate model-emitted entity_ids
    after parsing the response so hallucinated ids don't reach HA.

    Returns None if no system message is found, signalling "skip validation"
    to downstream callers.
    """
    try:
        data = json.loads(body_bytes.decode('utf-8'))
    except Exception:
        return None
    found = set()
    for msg in data.get('messages', []):
        if not isinstance(msg, dict):
            continue
        content = msg.get('content')
        if isinstance(content, str):
            for tok in _ENTITY_ID_TOKEN_RE.findall(content):
                found.add(tok)
    return found or None


def _clean_entity_id(value):
    """Strip trailing `,Friendly Name` suffix the model sometimes appends.

    HA's system prompt formats entities as CSV `entity_id,Friendly Name,state`
    and the model occasionally copies the friendly-name fragment into the
    entity_id value. Anything after the first valid `domain.object_id` token
    is dropped.
    """
    if not isinstance(value, str):
        return value
    m = _ENTITY_ID_RE.match(value)
    return m.group(1) if m else value


# Service names the model invents instead of HA's actual `light.turn_on` for
# brightness commands. All of these mean "turn_on with brightness_pct".
_BRIGHTNESS_SERVICE_ALIASES = {
    'set_brightness', 'set_brightness_pct', 'set_brightness_level',
    'dim', 'brighten', 'darken',
}

# Keys the model uses for brightness-percent. HA only accepts `brightness_pct`
# (0-100) on light.turn_on; `brightness` is 0-255 and the others are invented.
_BRIGHTNESS_KEY_ALIASES = (
    'brightness', 'value', 'new_value', 'new_level',
    'level', 'dim_level', 'percent', 'pct',
)


def _normalize_brightness(item):
    """Coerce variant brightness encodings into HA's canonical shape.

    Repairs the common qwen2.5:1.5b output mistakes for dim/set/at-N% commands:
      - service: "set_brightness" / "set_brightness_pct" → "turn_on"
      - service_data keys: value / new_value / brightness / level / percent → brightness_pct
      - brightness_pct emitted at item level (sibling of service_data) is
        moved INTO service_data so HA actually applies it
      - brightness > 100 (model treated it as 0-255) is rescaled to 0-100
    """
    if not isinstance(item, dict):
        return item

    # Promote misplaced item-level brightness keys into service_data
    sd = item.get('service_data')
    if not isinstance(sd, dict):
        sd = {}
        item['service_data'] = sd
    for k in _BRIGHTNESS_KEY_ALIASES + ('brightness_pct',):
        if k in item and k not in sd:
            sd[k] = item.pop(k)

    # Normalize service name
    svc = item.get('service')
    if svc in _BRIGHTNESS_SERVICE_ALIASES:
        item['service'] = 'turn_on'

    # Collapse alias keys onto brightness_pct
    for k in _BRIGHTNESS_KEY_ALIASES:
        if k in sd and 'brightness_pct' not in sd:
            sd['brightness_pct'] = sd.pop(k)
        elif k in sd:
            sd.pop(k, None)

    # If brightness_pct landed in 0-255 range (model output `brightness`),
    # rescale to 0-100. Anything > 100 is treated as a 0-255 value.
    bp = sd.get('brightness_pct')
    if isinstance(bp, (int, float)) and bp > 100:
        sd['brightness_pct'] = max(0, min(100, round(bp * 100 / 255)))

    return item


def _coalesce_list_items(items):
    """Merge / drop list entries to neutralise the "multi-action" failure mode.

    Observed: for single-device commands the model often emits 2-3 list items —
    one real action plus a bogus turn_off / dim of an unrelated real entity in
    HA's exposed list (Guest Room Stand Light is a frequent victim). This pass:

      1. Same-entity merge — combine plain `turn_on` and `turn_on`-with-
         brightness for the same entity into a single well-formed call.
      2. Cancelling-pair drop — `turn_off` of an entity that was just
         `turn_on`'d in the same call is removed.
      3. Cross-entity brightness rescue — if the first item lacks
         `brightness_pct` and a later item on a different entity has one,
         copy the value onto the first item before dropping the rest.
      4. Multi-distinct-entity drop — when distinct entity_ids remain, keep
         only the first item. The project scope is single-device commands;
         distinct second entities have always been the bogus pattern in
         observed traffic. (Loosen this rule if real multi-device usage
         appears.)
    """
    if not isinstance(items, list):
        return items

    # Step 1: merge same-entity items
    merged = []
    seen = {}  # (domain, entity_id) → index in merged

    for item in items:
        if not isinstance(item, dict):
            merged.append(item)
            continue
        sd = item.get('service_data') or {}
        key = (item.get('domain'), sd.get('entity_id'))
        if key[1] is None:
            merged.append(item)
            continue

        if key in seen:
            prior = merged[seen[key]]
            prior_sd = prior.setdefault('service_data', {})
            for k, v in sd.items():
                prior_sd.setdefault(k, v)
            if item.get('service') == 'turn_on' and 'brightness_pct' in sd:
                prior['service'] = 'turn_on'
        else:
            seen[key] = len(merged)
            merged.append(item)

    # Step 2: drop self-cancelling turn_off
    out = []
    on_targets = set()
    for item in merged:
        sd = item.get('service_data') or {}
        key = (item.get('domain'), sd.get('entity_id'))
        if item.get('service') == 'turn_on' and key[1] is not None:
            on_targets.add(key)
            out.append(item)
        elif item.get('service') == 'turn_off' and key in on_targets:
            continue
        else:
            out.append(item)

    # Step 3: cross-entity brightness rescue + multi-item drop
    if len(out) > 1:
        first = out[0]
        if isinstance(first, dict):
            first_sd = first.setdefault('service_data', {})
            if 'brightness_pct' not in first_sd:
                for later in out[1:]:
                    if not isinstance(later, dict):
                        continue
                    later_sd = later.get('service_data') or {}
                    bp = later_sd.get('brightness_pct')
                    if isinstance(bp, (int, float)):
                        first_sd['brightness_pct'] = bp
                        break
        out = [out[0]]

    return out


def _scrub_arguments(args):
    """Walk execute_services arguments and canonicalise entries in place.

    Cleanups applied:
      - entity_id stripped of `,Friendly Name` suffix
      - service / arg names normalised (set_brightness → turn_on, value → brightness_pct …)
      - misplaced item-level brightness_pct moved into service_data
      - same-entity items merged; self-cancelling turn_off after turn_on dropped
    """
    if not isinstance(args, dict):
        return args
    items = args.get('list')
    if not isinstance(items, list):
        return args

    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            cleaned.append(item)
            continue
        item = _normalize_brightness(item)
        sd = item.get('service_data')
        if isinstance(sd, dict) and 'entity_id' in sd:
            sd['entity_id'] = _clean_entity_id(sd['entity_id'])
        cleaned.append(item)

    args['list'] = _coalesce_list_items(cleaned)
    return args


def _try_parse_tool_call(text):
    """Extract (fn_name, arguments_dict) from model output, or return None.

    Handles these patterns the model may produce:
      {"name": "fn", "arguments": {...}}    ideal format matching our injection
      {"list": [...]}                        bare execute_services arguments
      fn_name({"list": [...]})               Python-style call notation
      ```json\\n{...}\\n```                  markdown-wrapped JSON
    Trailing commas and markdown fences are repaired before parsing, and
    duplicate service_data keys inside one object are merged.
    """
    text = _merge_duplicate_service_data(_fix_json(text.strip()))

    # Pattern: func_name({...})
    fn_match = re.match(r'^(\w+)\s*\((\{.+\})\)\s*$', text, re.DOTALL)
    if fn_match:
        try:
            inner = _merge_duplicate_service_data(_fix_json(fn_match.group(2)))
            return fn_match.group(1), _scrub_arguments(json.loads(inner))
        except Exception:
            pass

    # Pattern: pure JSON object
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            # {"name": "...", "arguments": {...}}
            if 'name' in parsed and 'arguments' in parsed:
                return parsed['name'], _scrub_arguments(parsed['arguments'])
            # {"list": [...]} — bare execute_services arguments
            if 'list' in parsed:
                return 'execute_services', _scrub_arguments(parsed)
    except Exception:
        pass

    return None


def _validate_tool_arguments(fn_name, arguments, known_entities=None):
    """Return True if the tool call arguments look actionable.

    Catches:
      - empty / missing fields (e.g. {"list": [{}]}) → HA raises
        "Unexpected error during intent recognition"
      - entity_id not present in HA's exposed-device list → HA returns
        "Unable to find entity ['<id>']" and the user hears that error
        spoken; we'd rather silence the reply than read an HA error aloud.

    `known_entities` is the set returned by extract_known_entities(); pass
    None to skip the entity-id allowlist check.
    """
    if fn_name == 'execute_services':
        items = arguments.get('list', [])
        if not items:
            return False
        for item in items:
            if not item.get('domain') or not item.get('service'):
                return False
            svc_data = item.get('service_data', {})
            entity_id = svc_data.get('entity_id')
            if not entity_id:
                return False
            if known_entities is not None and entity_id not in known_entities:
                sys.stderr.write(
                    '[proxy] entity_id {!r} not in HA exposed list — rejecting tool call\n'
                    .format(entity_id)
                )
                return False
    return True


def _looks_like_json(text):
    """Cheap heuristic: model output that opens with `{` or ```` ``` ```` is
    structured JSON we don't want HA's TTS reading aloud verbatim."""
    s = text.lstrip()
    return s.startswith('{') or s.startswith('```') or s.startswith('"{')


def rewrite_tool_response(body_bytes, known_entities=None):
    """hailo-tools-v1 (response side)

    hailo-ollama returns the model output as plain text in message.content.
    This layer parses that text, and if it looks like a valid tool call,
    rewrites the response into the OpenAI tool_calls format HA expects:
      - message.content → null
      - message.tool_calls → [{id, type, function: {name, arguments}}]
      - finish_reason → "tool_calls"

    If the content parses as a tool call but fails validation (e.g. empty
    service objects, hallucinated entity_id), or looks like JSON but cannot
    be parsed at all, the content is blanked to an empty string so HA's
    TTS does not read raw JSON or HA error messages aloud. Plain prose is
    passed through unchanged.

    `known_entities`: optional set of entity_ids HA exposed to the model
    (extracted from the request body); used to reject hallucinated ids.

    Returns `(new_body, status)` where status ∈ {'tool_call', 'rejected',
    'pass_through'}. Callers can use 'rejected' to decide whether to retry
    the request — the model's first attempt produced a tool-call shape that
    failed validation, and a single retry with perturbed sampling sometimes
    recovers. 'pass_through' means plain prose; retry won't help.
    'tool_call' means we emitted a valid tool_calls response.
    """
    try:
        resp = json.loads(body_bytes.decode('utf-8'))
    except Exception:
        return body_bytes, 'pass_through'

    choices = resp.get('choices', [])
    if not choices:
        return body_bytes, 'pass_through'

    choice = choices[0]
    message = choice.get('message', {})

    # Already a proper tool_calls response — nothing to do
    if message.get('tool_calls'):
        return body_bytes, 'tool_call'

    content = message.get('content') or ''
    result = _try_parse_tool_call(content)

    if result is not None:
        fn_name, arguments = result
        if _validate_tool_arguments(fn_name, arguments, known_entities):
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
            return json.dumps(resp).encode('utf-8'), 'tool_call'

        sys.stderr.write(
            '[proxy] tool call validation failed for {}: {}\n'
            .format(fn_name, json.dumps(arguments))
        )
        # Fall through to JSON-blanking below: we won't speak the bad call.

    if _looks_like_json(content):
        # Parsed-but-invalid or unparseable JSON-shaped output — silence it so
        # HA does not read "name execute services arguments list" aloud.
        sys.stderr.write('[proxy] suppressed JSON-shaped content from spoken response\n')
        message['content'] = ''
        choice['message'] = message
        resp['choices'] = [choice]
        return json.dumps(resp).encode('utf-8'), 'rejected'

    return body_bytes, 'pass_through'


def perturb_for_retry(body_bytes, temperature=0.7, top_p=0.95):
    """Bump sampling parameters on a previously-prepared request body.

    Used when the first attempt produced a structurally-bad tool call (empty
    list, hallucinated entity_id, …) and we want a second shot. Lifting
    temperature from HA's typical 0.1 to ~0.7 perturbs sampling enough that
    a deterministic-failure pattern (e.g. always emitting the same wrong
    Zigbee-style entity_id) often resolves on retry. Both the OpenAI-style
    top-level fields and the native Ollama `options` block are updated so
    the bump applies regardless of which path hailo-ollama uses internally.
    """
    try:
        data = json.loads(body_bytes.decode('utf-8'))
    except Exception:
        return body_bytes
    data['temperature'] = temperature
    data['top_p'] = top_p
    opts = data.get('options')
    if isinstance(opts, dict):
        opts['temperature'] = temperature
        opts['top_p'] = top_p
    return json.dumps(data).encode('utf-8')


# Sentence boundary: end-of-sentence punctuation followed by whitespace then
# (typically) a capital letter or end of string. Conservative — won't split on
# decimals or abbreviations because it requires a space + capital after.
_SENTENCE_END_RE = re.compile(r'([.!?])\s+(?=[A-Z])')


def truncate_followup_response(body_bytes):
    """Clean up the assistant reply on tool-result follow-up turns.

    Two forms of cleanup:
      1. JSON suppression — model occasionally re-emits a JSON tool call
         (e.g. `{"name":"execute_services","arguments":{"list":[]}}`)
         instead of natural language; that would be read aloud verbatim.
         Blank to "" so HA's TTS stays silent.
      2. First-sentence truncation — qwen2.5:1.5b reliably produces a
         correct opening sentence ("The office lights were turned on at
         70 percent brightness.") and then keeps writing, listing every
         entity from the system prompt and inventing details about each.
         Truncating after the first sentence keeps the accurate part and
         drops the hallucinations.
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
    content = message.get('content')
    if not isinstance(content, str) or not content.strip():
        return body_bytes

    if _looks_like_json(content):
        sys.stderr.write('[proxy] suppressed JSON-shaped content from follow-up reply\n')
        message['content'] = ''
        choice['message'] = message
        resp['choices'] = [choice]
        return json.dumps(resp).encode('utf-8')

    m = _SENTENCE_END_RE.search(content)
    if m:
        truncated = content[:m.end(1)].rstrip()
        if truncated and truncated != content.strip():
            message['content'] = truncated
            choice['message'] = message
            resp['choices'] = [choice]
            return json.dumps(resp).encode('utf-8')

    return body_bytes


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

    def _send_to_backend(self, body, label):
        """POST `body` to hailo-ollama and return (status_code, headers, response_bytes).

        Raises urllib.error.HTTPError or other Exception on transport failure.
        Used twice when retry-on-rejection fires.
        """
        req = urllib.request.Request(
            BACKEND + self.path, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in ('host', 'content-length', 'transfer-encoding'):
                req.add_header(k, v)
        _debug_log('REQ', '{} ({})'.format(self.command, label) if label else self.command,
                   self.path, body)
        r = urllib.request.urlopen(req, timeout=300)
        return r.status, r.headers, r.read()

    def _forward(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length) if length > 0 else None
        tool_mode = False  # False | True | 'followup'
        known_entities = None
        if body and self.headers.get('Content-Type', '').startswith('application/json'):
            body = fix_json_control_chars(body)
            body, tool_mode = inject_tool_prompt(body)       # extract tools, inject prompt
            if tool_mode is True:
                # Snapshot the entity allowlist before sanitisation strips
                # newlines / dedupes content; only used for tool turns.
                known_entities = extract_known_entities(body)
            body = sanitize_conversation_roles(body)         # fix null content / tool roles
            body = sanitize_for_hailo(body)                  # collapse newlines
            body = inject_defaults(body, self.path)
        try:
            status, headers, resp_body = self._send_to_backend(body, label='')
            content_type = headers.get('Content-Type', 'application/octet-stream')

            if tool_mode is True:
                resp_body, rewrite_status = rewrite_tool_response(resp_body, known_entities)
                # On rejection (validation failed → JSON blanked), take one more shot
                # with bumped sampling. The first failure is often a deterministic
                # mistake (e.g. always picking the same hallucinated entity_id) that
                # higher-temperature sampling shakes loose. Single retry, no recursion.
                if rewrite_status == 'rejected' and ARGS.retry_on_rejection:
                    sys.stderr.write(
                        '[proxy] retrying once with temperature=0.7 (first attempt rejected)\n'
                    )
                    retry_body = perturb_for_retry(body)
                    try:
                        _, _, retry_resp = self._send_to_backend(retry_body, label='retry')
                        retry_resp, retry_status = rewrite_tool_response(retry_resp, known_entities)
                        sys.stderr.write(
                            '[proxy] retry status: {}\n'.format(retry_status)
                        )
                        # Use whatever the retry produced — even if also rejected,
                        # the second blank is still better than the first's raw JSON.
                        resp_body = retry_resp
                    except Exception as exc:
                        sys.stderr.write('[proxy] retry forward error: ' + str(exc) + '\n')
                        # Keep the first-attempt rejected (blanked) body as fallback.
            elif tool_mode == 'followup':
                resp_body = truncate_followup_response(resp_body)

            _debug_log('RES', status, self.path, resp_body)
            self._send(status, resp_body, content_type)
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
        '{temp}{topp}{retry}{debug}\n'.format(
            listen=LISTEN_PORT,
            backend=BACKEND_PORT,
            mt=ARGS.max_tokens,
            np=ARGS.num_predict,
            nc=ARGS.num_ctx,
            temp=' temperature={}'.format(ARGS.temperature) if ARGS.temperature is not None else '',
            topp=' top_p={}'.format(ARGS.top_p) if ARGS.top_p is not None else '',
            retry='' if ARGS.retry_on_rejection else ' retry-on-rejection=off',
            debug='' if ARGS.log_level == 'info' else ' log-level={}'.format(ARGS.log_level),
        )
    )
    http.server.HTTPServer(('0.0.0.0', LISTEN_PORT), Handler).serve_forever()
