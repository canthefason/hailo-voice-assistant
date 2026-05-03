"""Unit tests for proxy.py — all pure / near-pure functions.

Importing strategy: proxy.py runs _parse_args() and _load_prompt_config() at
module level.  We clear sys.argv before import so argparse sees no flags and
uses defaults, then patch proxy.ARGS / proxy.PROMPT_CONFIG per-test when the
default values would interfere.
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

# Ensure proxy is importable from this directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.argv = ['proxy']          # suppress argparse picking up pytest flags

import proxy                  # noqa: E402  (must come after sys.argv reset)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _j(obj):
    """Encode obj to UTF-8 bytes."""
    return json.dumps(obj).encode('utf-8')


def _d(b):
    """Decode bytes back to a Python object."""
    return json.loads(b.decode('utf-8'))


def _default_args(**kwargs):
    """Return a SimpleNamespace that mimics proxy.ARGS defaults."""
    defaults = dict(
        max_tokens=120,
        num_predict=60,
        num_ctx=1024,
        temperature=None,
        top_p=None,
        log_level='info',
        retry_on_rejection=True,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# fix_json_control_chars
# ---------------------------------------------------------------------------

class TestFixJsonControlChars(unittest.TestCase):

    def _run(self, raw: str) -> str:
        return proxy.fix_json_control_chars(raw.encode('utf-8')).decode('utf-8')

    def test_lf_inside_string_escaped(self):
        raw = '{"msg": "hello\nworld"}'
        out = self._run(raw)
        # The raw output must contain the \\n escape sequence (not a literal LF inside the string).
        self.assertIn(r'\n', out)
        # And it must be valid JSON.
        self.assertEqual(json.loads(out)['msg'], 'hello\nworld')

    def test_cr_inside_string_escaped(self):
        raw = '{"msg": "a\rb"}'
        out = self._run(raw)
        self.assertIn(r'\r', out)

    def test_tab_inside_string_escaped(self):
        raw = '{"msg": "a\tb"}'
        out = self._run(raw)
        self.assertIn(r'\t', out)

    def test_lf_outside_string_kept(self):
        raw = '{"a": 1,\n"b": 2}'
        out = self._run(raw)
        self.assertIn('\n', out)   # structural newline preserved
        self.assertEqual(json.loads(out), {'a': 1, 'b': 2})

    def test_already_escaped_not_double_escaped(self):
        raw = r'{"msg": "line1\\nline2"}'
        out = self._run(raw)
        # The \\n is an escaped backslash + 'n' — should stay as-is
        self.assertEqual(json.loads(out)['msg'], 'line1\\nline2')

    def test_other_control_char_escaped(self):
        # U+0001 (SOH)
        raw = '{"msg": "a\x01b"}'
        out = self._run(raw)
        self.assertIn('\\u0001', out)

    def test_clean_json_unchanged(self):
        raw = '{"key": "value", "num": 42}'
        self.assertEqual(self._run(raw), raw)

    def test_invalid_utf8_returned_as_is(self):
        bad = b'\xff\xfe'
        result = proxy.fix_json_control_chars(bad)
        self.assertEqual(result, bad)


# ---------------------------------------------------------------------------
# sanitize_for_hailo
# ---------------------------------------------------------------------------

class TestSanitizeForHailo(unittest.TestCase):

    def test_newline_in_message_content_replaced(self):
        body = _j({'messages': [{'role': 'user', 'content': 'hello\nworld'}]})
        out = _d(proxy.sanitize_for_hailo(body))
        self.assertEqual(out['messages'][0]['content'], 'hello world')

    def test_newline_in_prompt_field_replaced(self):
        body = _j({'prompt': 'line1\nline2'})
        out = _d(proxy.sanitize_for_hailo(body))
        self.assertEqual(out['prompt'], 'line1 line2')

    def test_newline_in_system_field_replaced(self):
        body = _j({'system': 'sys\nnewline'})
        out = _d(proxy.sanitize_for_hailo(body))
        self.assertEqual(out['system'], 'sys newline')

    def test_tab_replaced(self):
        body = _j({'messages': [{'role': 'user', 'content': 'a\tb'}]})
        out = _d(proxy.sanitize_for_hailo(body))
        self.assertEqual(out['messages'][0]['content'], 'a b')

    def test_clean_body_unchanged(self):
        original = _j({'messages': [{'role': 'user', 'content': 'clean'}]})
        result = proxy.sanitize_for_hailo(original)
        self.assertEqual(result, original)

    def test_invalid_json_returned_as_is(self):
        bad = b'not json'
        self.assertEqual(proxy.sanitize_for_hailo(bad), bad)

    def test_non_string_content_untouched(self):
        body = _j({'messages': [{'role': 'user', 'content': None}]})
        out = _d(proxy.sanitize_for_hailo(body))
        self.assertIsNone(out['messages'][0]['content'])


# ---------------------------------------------------------------------------
# inject_defaults
# ---------------------------------------------------------------------------

class TestInjectDefaults(unittest.TestCase):

    def _run(self, obj, path='/v1/chat/completions', **arg_overrides):
        args = _default_args(**arg_overrides)
        with patch.object(proxy, 'ARGS', args):
            return _d(proxy.inject_defaults(_j(obj), path))

    # /v1/chat/completions ------------------------------------------------

    def test_max_tokens_injected_when_missing(self):
        out = self._run({}, max_tokens=120)
        self.assertEqual(out['max_tokens'], 120)

    def test_max_tokens_clamped_when_pathologically_high(self):
        out = self._run({'max_tokens': 1022}, max_tokens=120)
        self.assertEqual(out['max_tokens'], 120)

    def test_max_tokens_left_alone_when_reasonable(self):
        out = self._run({'max_tokens': 200}, max_tokens=120)
        self.assertEqual(out['max_tokens'], 200)

    def test_num_ctx_injected_when_missing(self):
        out = self._run({}, num_ctx=1024)
        self.assertEqual(out['num_ctx'], 1024)

    def test_temperature_injected_when_set(self):
        out = self._run({}, temperature=0.3)
        self.assertAlmostEqual(out['temperature'], 0.3)

    def test_temperature_not_injected_when_none(self):
        out = self._run({}, temperature=None)
        self.assertNotIn('temperature', out)

    def test_top_p_injected_when_set(self):
        out = self._run({}, top_p=0.9)
        self.assertAlmostEqual(out['top_p'], 0.9)

    # /api/generate + /api/chat -------------------------------------------

    def test_api_generate_num_predict_injected(self):
        out = self._run({}, path='/api/generate', num_predict=60)
        self.assertEqual(out['options']['num_predict'], 60)

    def test_api_chat_num_predict_injected(self):
        out = self._run({}, path='/api/chat', num_predict=60)
        self.assertEqual(out['options']['num_predict'], 60)

    def test_api_generate_num_ctx_injected(self):
        out = self._run({}, path='/api/generate', num_ctx=512)
        self.assertEqual(out['options']['num_ctx'], 512)

    def test_api_generate_temperature_injected_when_set(self):
        out = self._run({}, path='/api/generate', temperature=0.5)
        self.assertAlmostEqual(out['options']['temperature'], 0.5)

    def test_unknown_path_unchanged(self):
        original = _j({'x': 1})
        args = _default_args()
        with patch.object(proxy, 'ARGS', args):
            result = proxy.inject_defaults(original, '/unknown')
        self.assertEqual(result, original)

    def test_invalid_json_returned_as_is(self):
        bad = b'not json'
        args = _default_args()
        with patch.object(proxy, 'ARGS', args):
            self.assertEqual(proxy.inject_defaults(bad, '/v1/chat/completions'), bad)


# ---------------------------------------------------------------------------
# sanitize_conversation_roles
# ---------------------------------------------------------------------------

class TestSanitizeConversationRoles(unittest.TestCase):

    def test_assistant_tool_calls_null_content_rewritten(self):
        body = _j({'messages': [{
            'role': 'assistant',
            'content': None,
            'tool_calls': [{'function': {'name': 'execute_services', 'arguments': '{"list":[]}'}}],
        }]})
        out = _d(proxy.sanitize_conversation_roles(body))
        msg = out['messages'][0]
        self.assertEqual(msg['role'], 'assistant')
        self.assertIsInstance(msg['content'], str)
        self.assertNotIn('tool_calls', msg)

    def test_tool_role_rewritten_to_user(self):
        body = _j({'messages': [{'role': 'tool', 'name': 'execute_services', 'content': 'ok'}]})
        out = _d(proxy.sanitize_conversation_roles(body))
        msg = out['messages'][0]
        self.assertEqual(msg['role'], 'user')
        self.assertIn('execute_services', msg['content'])
        self.assertIn('ok', msg['content'])

    def test_normal_messages_unchanged(self):
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': 'hello'},
        ]
        body = _j({'messages': messages})
        out = _d(proxy.sanitize_conversation_roles(body))
        self.assertEqual(out['messages'], messages)

    def test_invalid_json_returned_as_is(self):
        bad = b'not json'
        self.assertEqual(proxy.sanitize_conversation_roles(bad), bad)


# ---------------------------------------------------------------------------
# _is_tool_result_followup
# ---------------------------------------------------------------------------

class TestIsToolResultFollowup(unittest.TestCase):

    def test_true_when_tool_role_present(self):
        msgs = [{'role': 'user', 'content': 'x'}, {'role': 'tool', 'content': 'y'}]
        self.assertTrue(proxy._is_tool_result_followup(msgs))

    def test_false_when_no_tool_role(self):
        msgs = [{'role': 'user', 'content': 'x'}, {'role': 'assistant', 'content': 'y'}]
        self.assertFalse(proxy._is_tool_result_followup(msgs))

    def test_false_on_empty_list(self):
        self.assertFalse(proxy._is_tool_result_followup([]))


# ---------------------------------------------------------------------------
# _fix_json
# ---------------------------------------------------------------------------

class TestFixJson(unittest.TestCase):

    def test_strips_markdown_fence(self):
        text = '```json\n{"a": 1}\n```'
        self.assertEqual(json.loads(proxy._fix_json(text)), {'a': 1})

    def test_strips_surrounding_quotes(self):
        text = '\'{"a": 1}\''
        self.assertEqual(json.loads(proxy._fix_json(text)), {'a': 1})

    def test_removes_trailing_comma_before_brace(self):
        text = '{"a": 1,}'
        self.assertEqual(json.loads(proxy._fix_json(text)), {'a': 1})

    def test_removes_trailing_comma_before_bracket(self):
        text = '[1, 2,]'
        self.assertEqual(json.loads(proxy._fix_json(text)), [1, 2])

    def test_balances_missing_closing_brace(self):
        text = '{"a": 1'
        result = proxy._fix_json(text)
        self.assertEqual(json.loads(result), {'a': 1})

    def test_balances_nested_missing_braces(self):
        text = '{"a": {"b": 1'
        result = proxy._fix_json(text)
        self.assertEqual(json.loads(result), {'a': {'b': 1}})

    def test_clean_json_unchanged(self):
        text = '{"a": 1}'
        self.assertEqual(proxy._fix_json(text), text)


# ---------------------------------------------------------------------------
# _merge_duplicate_service_data
# ---------------------------------------------------------------------------

class TestMergeDuplicateServiceData(unittest.TestCase):

    def test_merges_two_service_data_blocks(self):
        text = (
            '{"domain": "light", "service": "turn_on", '
            '"service_data": {"entity_id": "light.x"}, '
            '"service_data": {"brightness_pct": 50}}'
        )
        result = proxy._merge_duplicate_service_data(text)
        obj = json.loads(result)
        self.assertEqual(obj['service_data']['entity_id'], 'light.x')
        self.assertEqual(obj['service_data']['brightness_pct'], 50)

    def test_no_duplicates_unchanged(self):
        text = '{"service_data": {"entity_id": "light.x"}}'
        self.assertEqual(proxy._merge_duplicate_service_data(text), text)


# ---------------------------------------------------------------------------
# extract_known_entities
# ---------------------------------------------------------------------------

class TestExtractKnownEntities(unittest.TestCase):

    def test_extracts_entities_from_system_message(self):
        body = _j({'messages': [
            {'role': 'system', 'content': 'light.kitchen,Kitchen,on light.bedroom,Bedroom,off'},
            {'role': 'user', 'content': 'turn on lights'},
        ]})
        entities = proxy.extract_known_entities(body)
        self.assertIn('light.kitchen', entities)
        self.assertIn('light.bedroom', entities)

    def test_extracts_from_all_messages(self):
        body = _j({'messages': [
            {'role': 'user', 'content': 'switch.fan is on'},
        ]})
        entities = proxy.extract_known_entities(body)
        self.assertIn('switch.fan', entities)

    def test_returns_none_on_invalid_json(self):
        self.assertIsNone(proxy.extract_known_entities(b'not json'))

    def test_returns_none_when_no_entities_found(self):
        body = _j({'messages': [{'role': 'user', 'content': 'hello'}]})
        self.assertIsNone(proxy.extract_known_entities(body))


# ---------------------------------------------------------------------------
# _clean_entity_id
# ---------------------------------------------------------------------------

class TestCleanEntityId(unittest.TestCase):

    def test_strips_friendly_name_suffix(self):
        self.assertEqual(proxy._clean_entity_id('light.x,Kitchen Light'), 'light.x')

    def test_bare_entity_id_unchanged(self):
        self.assertEqual(proxy._clean_entity_id('light.kitchen'), 'light.kitchen')

    def test_non_string_returned_as_is(self):
        self.assertIsNone(proxy._clean_entity_id(None))
        self.assertEqual(proxy._clean_entity_id(42), 42)


# ---------------------------------------------------------------------------
# _normalize_brightness
# ---------------------------------------------------------------------------

class TestNormalizeBrightness(unittest.TestCase):

    def test_set_brightness_service_renamed_to_turn_on(self):
        item = {'domain': 'light', 'service': 'set_brightness',
                'service_data': {'entity_id': 'light.x', 'brightness_pct': 50}}
        out = proxy._normalize_brightness(item)
        self.assertEqual(out['service'], 'turn_on')

    def test_alias_brightness_key_renamed(self):
        item = {'domain': 'light', 'service': 'turn_on',
                'service_data': {'entity_id': 'light.x', 'value': 70}}
        out = proxy._normalize_brightness(item)
        self.assertEqual(out['service_data']['brightness_pct'], 70)
        self.assertNotIn('value', out['service_data'])

    def test_brightness_over_100_rescaled(self):
        # 128/255 ≈ 50%
        item = {'domain': 'light', 'service': 'turn_on',
                'service_data': {'entity_id': 'light.x', 'brightness_pct': 128}}
        out = proxy._normalize_brightness(item)
        bp = out['service_data']['brightness_pct']
        self.assertGreaterEqual(bp, 40)
        self.assertLessEqual(bp, 60)

    def test_brightness_pct_at_item_level_moved_to_service_data(self):
        item = {'domain': 'light', 'service': 'turn_on',
                'brightness_pct': 40,
                'service_data': {'entity_id': 'light.x'}}
        out = proxy._normalize_brightness(item)
        self.assertEqual(out['service_data']['brightness_pct'], 40)
        self.assertNotIn('brightness_pct', {k: v for k, v in out.items() if k != 'service_data'})

    def test_non_dict_returned_as_is(self):
        self.assertEqual(proxy._normalize_brightness('bad'), 'bad')


# ---------------------------------------------------------------------------
# _coalesce_list_items
# ---------------------------------------------------------------------------

class TestCoalesceListItems(unittest.TestCase):

    def _item(self, entity, service='turn_on', **sd_extra):
        sd = {'entity_id': entity}
        sd.update(sd_extra)
        return {'domain': 'light', 'service': service, 'service_data': sd}

    def test_same_entity_items_merged(self):
        items = [
            self._item('light.x'),
            self._item('light.x', brightness_pct=50),
        ]
        out = proxy._coalesce_list_items(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['service_data']['brightness_pct'], 50)

    def test_cancelling_turn_off_dropped(self):
        items = [
            self._item('light.x', 'turn_on'),
            self._item('light.x', 'turn_off'),
        ]
        out = proxy._coalesce_list_items(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['service'], 'turn_on')

    def test_cross_entity_brightness_rescued(self):
        items = [
            self._item('light.x'),                        # no brightness
            self._item('light.y', brightness_pct=30),     # has brightness
        ]
        out = proxy._coalesce_list_items(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['service_data']['entity_id'], 'light.x')
        self.assertEqual(out[0]['service_data']['brightness_pct'], 30)

    def test_multiple_distinct_entities_keeps_first(self):
        items = [self._item('light.a'), self._item('light.b'), self._item('light.c')]
        out = proxy._coalesce_list_items(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['service_data']['entity_id'], 'light.a')

    def test_non_list_returned_as_is(self):
        self.assertEqual(proxy._coalesce_list_items('x'), 'x')


# ---------------------------------------------------------------------------
# _try_parse_tool_call
# ---------------------------------------------------------------------------

class TestTryParseToolCall(unittest.TestCase):

    def _valid_execute(self, entity='light.x', service='turn_on', brightness=None):
        sd = {'entity_id': entity}
        if brightness is not None:
            sd['brightness_pct'] = brightness
        return {'list': [{'domain': 'light', 'service': service, 'service_data': sd}]}

    def test_parses_name_arguments_format(self):
        text = json.dumps({'name': 'execute_services', 'arguments': self._valid_execute()})
        result = proxy._try_parse_tool_call(text)
        self.assertIsNotNone(result)
        fn, _ = result
        self.assertEqual(fn, 'execute_services')

    def test_parses_bare_list_format(self):
        text = json.dumps(self._valid_execute())
        result = proxy._try_parse_tool_call(text)
        self.assertIsNotNone(result)
        fn, _ = result
        self.assertEqual(fn, 'execute_services')

    def test_parses_function_call_notation(self):
        text = 'execute_services({"list": [{"domain": "light", "service": "turn_on", "service_data": {"entity_id": "light.x"}}]})'
        result = proxy._try_parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 'execute_services')

    def test_parses_markdown_fenced_json(self):
        inner = json.dumps(self._valid_execute())
        text = '```json\n' + inner + '\n```'
        result = proxy._try_parse_tool_call(text)
        self.assertIsNotNone(result)

    def test_returns_none_for_plain_prose(self):
        self.assertIsNone(proxy._try_parse_tool_call('The lights are on.'))

    def test_repairs_trailing_comma(self):
        text = '{"name": "execute_services", "arguments": {"list": [{"domain": "light", "service": "turn_on", "service_data": {"entity_id": "light.x"},}]}}'
        result = proxy._try_parse_tool_call(text)
        self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# _validate_tool_arguments
# ---------------------------------------------------------------------------

class TestValidateToolArguments(unittest.TestCase):

    def _args(self, entity='light.x', domain='light', service='turn_on', **sd_extra):
        sd = {'entity_id': entity}
        sd.update(sd_extra)
        return {'list': [{'domain': domain, 'service': service, 'service_data': sd}]}

    def test_valid_call_returns_true(self):
        self.assertTrue(proxy._validate_tool_arguments('execute_services', self._args()))

    def test_empty_list_returns_false(self):
        self.assertFalse(proxy._validate_tool_arguments('execute_services', {'list': []}))

    def test_missing_domain_returns_false(self):
        args = {'list': [{'service': 'turn_on', 'service_data': {'entity_id': 'light.x'}}]}
        self.assertFalse(proxy._validate_tool_arguments('execute_services', args))

    def test_missing_service_returns_false(self):
        args = {'list': [{'domain': 'light', 'service_data': {'entity_id': 'light.x'}}]}
        self.assertFalse(proxy._validate_tool_arguments('execute_services', args))

    def test_missing_entity_id_returns_false(self):
        args = {'list': [{'domain': 'light', 'service': 'turn_on', 'service_data': {}}]}
        self.assertFalse(proxy._validate_tool_arguments('execute_services', args))

    def test_hallucinated_entity_rejected_when_known_entities_given(self):
        known = {'light.real'}
        self.assertFalse(proxy._validate_tool_arguments(
            'execute_services', self._args(entity='light.fake'), known
        ))

    def test_valid_entity_passes_allowlist_check(self):
        known = {'light.x'}
        self.assertTrue(proxy._validate_tool_arguments(
            'execute_services', self._args(entity='light.x'), known
        ))

    def test_non_execute_services_always_returns_true(self):
        self.assertTrue(proxy._validate_tool_arguments('other_fn', {}))


# ---------------------------------------------------------------------------
# _looks_like_json
# ---------------------------------------------------------------------------

class TestLooksLikeJson(unittest.TestCase):

    def test_true_for_opening_brace(self):
        self.assertTrue(proxy._looks_like_json('{"name": "x"}'))

    def test_true_for_markdown_fence(self):
        self.assertTrue(proxy._looks_like_json('```json\n{}'))

    def test_true_for_quoted_brace(self):
        self.assertTrue(proxy._looks_like_json('"{'))

    def test_false_for_plain_prose(self):
        self.assertFalse(proxy._looks_like_json('The lights are on.'))

    def test_leading_whitespace_ignored(self):
        self.assertTrue(proxy._looks_like_json('   {"a": 1}'))


# ---------------------------------------------------------------------------
# rewrite_tool_response
# ---------------------------------------------------------------------------

class TestRewriteToolResponse(unittest.TestCase):

    def _response(self, content):
        return _j({
            'choices': [{'message': {'role': 'assistant', 'content': content},
                         'finish_reason': 'stop'}]
        })

    def _valid_tool_json(self, entity='light.x'):
        return json.dumps({
            'name': 'execute_services',
            'arguments': {
                'list': [{'domain': 'light', 'service': 'turn_on',
                          'service_data': {'entity_id': entity}}]
            }
        })

    def test_valid_tool_call_rewritten(self):
        body = self._response(self._valid_tool_json())
        out, status = proxy.rewrite_tool_response(body, known_entities={'light.x'})
        self.assertEqual(status, 'tool_call')
        data = _d(out)
        tc = data['choices'][0]['message']['tool_calls']
        self.assertEqual(tc[0]['function']['name'], 'execute_services')
        self.assertIsNone(data['choices'][0]['message']['content'])

    def test_plain_prose_passes_through(self):
        body = self._response('The lights are on.')
        out, status = proxy.rewrite_tool_response(body)
        self.assertEqual(status, 'pass_through')
        self.assertEqual(out, body)

    def test_json_shaped_invalid_content_blanked(self):
        body = self._response('{"list": [{}]}')
        out, status = proxy.rewrite_tool_response(body)
        self.assertEqual(status, 'rejected')
        data = _d(out)
        self.assertEqual(data['choices'][0]['message']['content'], '')

    def test_hallucinated_entity_blanked(self):
        body = self._response(self._valid_tool_json(entity='light.fake'))
        _, status = proxy.rewrite_tool_response(body, known_entities={'light.real'})
        self.assertEqual(status, 'rejected')

    def test_already_tool_calls_untouched(self):
        body = _j({'choices': [{'message': {
            'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'c0', 'type': 'function',
                            'function': {'name': 'f', 'arguments': '{}'}}]
        }, 'finish_reason': 'tool_calls'}]})
        out, status = proxy.rewrite_tool_response(body)
        self.assertEqual(status, 'tool_call')
        self.assertEqual(out, body)

    def test_invalid_json_body_pass_through(self):
        _, status = proxy.rewrite_tool_response(b'not json')
        self.assertEqual(status, 'pass_through')

    def test_empty_choices_pass_through(self):
        body = _j({'choices': []})
        _, status = proxy.rewrite_tool_response(body)
        self.assertEqual(status, 'pass_through')


# ---------------------------------------------------------------------------
# perturb_for_retry
# ---------------------------------------------------------------------------

class TestPerturbForRetry(unittest.TestCase):

    def test_sets_temperature_and_top_p(self):
        body = _j({'messages': []})
        out = _d(proxy.perturb_for_retry(body, temperature=0.7, top_p=0.95))
        self.assertAlmostEqual(out['temperature'], 0.7)
        self.assertAlmostEqual(out['top_p'], 0.95)

    def test_also_updates_options_block(self):
        body = _j({'options': {'num_ctx': 1024}})
        out = _d(proxy.perturb_for_retry(body, temperature=0.7, top_p=0.95))
        self.assertAlmostEqual(out['options']['temperature'], 0.7)
        self.assertAlmostEqual(out['options']['top_p'], 0.95)

    def test_invalid_json_returned_as_is(self):
        bad = b'not json'
        self.assertEqual(proxy.perturb_for_retry(bad), bad)


# ---------------------------------------------------------------------------
# truncate_followup_response
# ---------------------------------------------------------------------------

class TestTruncateFollowupResponse(unittest.TestCase):

    def _response(self, content):
        return _j({'choices': [{'message': {'role': 'assistant', 'content': content},
                                'finish_reason': 'stop'}]})

    def test_truncates_after_first_sentence(self):
        body = self._response('The lights are on. They are very bright. More stuff.')
        out = _d(proxy.truncate_followup_response(body))
        self.assertEqual(out['choices'][0]['message']['content'], 'The lights are on.')

    def test_single_sentence_unchanged(self):
        body = self._response('Done.')
        out = _d(proxy.truncate_followup_response(body))
        self.assertEqual(out['choices'][0]['message']['content'], 'Done.')

    def test_json_content_blanked(self):
        body = self._response('{"name": "execute_services"}')
        out = _d(proxy.truncate_followup_response(body))
        self.assertEqual(out['choices'][0]['message']['content'], '')

    def test_empty_content_unchanged(self):
        body = self._response('')
        result = proxy.truncate_followup_response(body)
        self.assertEqual(result, body)

    def test_invalid_json_returned_as_is(self):
        bad = b'not json'
        self.assertEqual(proxy.truncate_followup_response(bad), bad)


# ---------------------------------------------------------------------------
# _load_prompt_config
# ---------------------------------------------------------------------------

class TestLoadPromptConfig(unittest.TestCase):

    def _write_config(self, data):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(data, f)
        f.close()
        return f.name

    def test_loads_all_keys(self):
        path = self._write_config({
            'example': 'ex', 'instruction_template': 'inst', 'followup_hint': 'hint'
        })
        cfg = proxy._load_prompt_config(path)
        self.assertEqual(cfg['example'], 'ex')
        self.assertEqual(cfg['instruction_template'], 'inst')
        self.assertEqual(cfg['followup_hint'], 'hint')
        os.unlink(path)

    def test_missing_file_returns_empty_defaults(self):
        cfg = proxy._load_prompt_config('/nonexistent/path.json')
        self.assertEqual(cfg['example'], '')
        self.assertEqual(cfg['instruction_template'], '')
        self.assertEqual(cfg['followup_hint'], '')

    def test_non_string_value_ignored(self):
        path = self._write_config({'example': ['list', 'not', 'str']})
        cfg = proxy._load_prompt_config(path)
        self.assertEqual(cfg['example'], '')
        os.unlink(path)

    def test_unknown_keys_ignored(self):
        path = self._write_config({'example': 'ex', 'unknown_key': 'val'})
        cfg = proxy._load_prompt_config(path)
        self.assertNotIn('unknown_key', cfg)
        os.unlink(path)

    def test_none_path_returns_empty_defaults(self):
        cfg = proxy._load_prompt_config(None)
        self.assertEqual(cfg, {'example': '', 'instruction_template': '', 'followup_hint': ''})


# ---------------------------------------------------------------------------
# inject_tool_prompt
# ---------------------------------------------------------------------------

class TestInjectToolPrompt(unittest.TestCase):

    def _prompt_config(self, template='RULES: {tool_desc} {example}',
                       example='ex', followup_hint=' followup'):
        return {'instruction_template': template, 'example': example,
                'followup_hint': followup_hint}

    def _body_with_tools(self, messages=None, tools=None):
        if messages is None:
            messages = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'hi'}]
        if tools is None:
            tools = [{'function': {'name': 'execute_services', 'description': 'run services'}}]
        return _j({'messages': messages, 'tools': tools})

    def test_tools_removed_from_body(self):
        body = self._body_with_tools()
        with patch.object(proxy, 'PROMPT_CONFIG', self._prompt_config()):
            with patch.object(proxy, 'ARGS', _default_args()):
                out, _ = proxy.inject_tool_prompt(body)
        self.assertNotIn('tools', _d(out))
        self.assertNotIn('tool_choice', _d(out))

    def test_had_tools_true_on_normal_turn(self):
        body = self._body_with_tools()
        with patch.object(proxy, 'PROMPT_CONFIG', self._prompt_config()):
            with patch.object(proxy, 'ARGS', _default_args()):
                _, had_tools = proxy.inject_tool_prompt(body)
        self.assertTrue(had_tools)

    def test_instruction_injected_into_system_message(self):
        body = self._body_with_tools()
        with patch.object(proxy, 'PROMPT_CONFIG', self._prompt_config(template='INST {tool_desc} {example}')):
            with patch.object(proxy, 'ARGS', _default_args()):
                out, _ = proxy.inject_tool_prompt(body)
        sys_msg = next(m for m in _d(out)['messages'] if m['role'] == 'system')
        self.assertIn('INST', sys_msg['content'])
        self.assertIn('execute_services', sys_msg['content'])

    def test_followup_turn_returns_followup_sentinel(self):
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'tool', 'content': 'result'},
        ]
        body = self._body_with_tools(messages=messages)
        with patch.object(proxy, 'PROMPT_CONFIG', self._prompt_config()):
            with patch.object(proxy, 'ARGS', _default_args()):
                _, had_tools = proxy.inject_tool_prompt(body)
        self.assertEqual(had_tools, 'followup')

    def test_followup_hint_appended_on_followup_turn(self):
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'tool', 'content': 'done'},
        ]
        body = self._body_with_tools(messages=messages)
        with patch.object(proxy, 'PROMPT_CONFIG', self._prompt_config(followup_hint=' HINT')):
            with patch.object(proxy, 'ARGS', _default_args()):
                out, _ = proxy.inject_tool_prompt(body)
        sys_msg = next(m for m in _d(out)['messages'] if m['role'] == 'system')
        self.assertIn('HINT', sys_msg['content'])

    def test_no_tools_in_request_returns_unchanged(self):
        body = _j({'messages': [{'role': 'user', 'content': 'hi'}]})
        with patch.object(proxy, 'PROMPT_CONFIG', self._prompt_config()):
            with patch.object(proxy, 'ARGS', _default_args()):
                out, had_tools = proxy.inject_tool_prompt(body)
        self.assertFalse(had_tools)
        self.assertEqual(out, body)

    def test_empty_template_skips_injection(self):
        body = self._body_with_tools()
        with patch.object(proxy, 'PROMPT_CONFIG', self._prompt_config(template='')):
            with patch.object(proxy, 'ARGS', _default_args()):
                out, had_tools = proxy.inject_tool_prompt(body)
        self.assertTrue(had_tools)
        sys_msg = next(m for m in _d(out)['messages'] if m['role'] == 'system')
        self.assertEqual(sys_msg['content'], 'sys')  # unchanged

    def test_max_tokens_raised_for_tool_calls(self):
        body = self._body_with_tools()
        with patch.object(proxy, 'PROMPT_CONFIG', self._prompt_config()):
            with patch.object(proxy, 'ARGS', _default_args(max_tokens=120)):
                out, _ = proxy.inject_tool_prompt(body)
        self.assertGreaterEqual(_d(out).get('max_tokens', 0), 250)


if __name__ == '__main__':
    unittest.main()
