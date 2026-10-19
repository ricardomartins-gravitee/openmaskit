import asyncio
import copy
import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import AsyncMock, patch


from openmaskit.masking.engine import MaskingEngine
from openmaskit.masking.mappers import ResponseMapper
from openmaskit.masking.parsing import DEFAULT_MAX_PARSE_LEN, try_parse_structured
from openmaskit.masking.rules import MaskingRule
from openmaskit.masking.store import MaskingStore

PATHS = ['author', 'recipients', 'ccList', 'messages.author', 'messages.recipients', 'messages.ccList']

def make_engine(rules=None):
    return MaskingEngine(
        [MaskingRule('*', p, 'header') for p in PATHS] if rules is None else rules,
        None,
    )

def envelope(data):
    return {'content': [{'type': 'text', 'text': json.dumps(data)}]}

def payload(result):
    return json.loads(result['content'][0]['text'])

class PrivacyRegressionTests(unittest.TestCase):
    def test_empty_headers_remain_empty_without_aliases(self):
        e = make_engine()
        result = payload(e.mask_response('getMessage', envelope({'author': '', 'recipients': '', 'ccList': ''})))
        self.assertEqual(result, {'author': '', 'recipients': '', 'ccList': ''})
        self.assertEqual(e.pending_writes_count, 0)
        self.assertEqual(e.alias_cache, {})

    def test_terminal_lists_preserve_empty_strings_and_types(self):
        e = make_engine()
        result = e.mask_response('getMessage', {'structuredContent': {'recipients': ['', 'Bob', 2, False, None]}})
        self.assertEqual(result['structuredContent']['recipients'], ['', 'header_1', 2, False, None])
        self.assertNotIn('', e.alias_cache.values())

    def test_legacy_empty_mapping_cannot_expand_plain_text(self):
        e = make_engine()
        store = AsyncMock()
        store.get_all_aliases.return_value = {'header_1': '', 'header_2': 'Alice Smith'}
        e._store = store
        asyncio.run(e.load_aliases())
        result = e.mask_response('getMessage', {'content': [{'type': 'text', 'text': 'Alice Smith called.'}]})
        self.assertTrue(result['content'][0]['text'] == 'header_2 called.')
        self.assertEqual(e.unmask_arguments('sendMail', {'to': 'header_1'}), {'to': ''})

    def test_existing_empty_mapping_is_not_reused_for_new_empty_field(self):
        e = make_engine()
        e._reverse_cache = {'header': {'': 'header_99'}}
        result = payload(e.mask_response('getMessage', envelope({'ccList': ''})))
        self.assertEqual(result['ccList'], '')
        self.assertEqual(e.pending_writes_count, 0)

    def test_six_header_paths_and_reversible_aliases(self):
        row = {'id': 'synthetic-id', 'author': 'Alice Smith <alice@example.test>',
               'recipients': 'Bob Brown <bob@example.test>', 'ccList': '',
               'subject': 'Alice update', 'body': 'Hi Bob', 'name': 'Inbox'}
        for data in [row, [row], {'messages': [row]}, [[row]]]:
            with self.subTest(shape=type(data).__name__):
                e = make_engine()
                result = payload(e.mask_response('searchMessages', envelope(copy.deepcopy(data))))
                r = result['messages'][0] if isinstance(result, dict) and 'messages' in result else result
                while isinstance(r, list):
                    r = r[0]
                self.assertEqual(r['author'], 'header_1')
                self.assertEqual(r['recipients'], 'header_2')
                self.assertEqual(r['ccList'], '')
                for key in ['id', 'subject', 'body', 'name']:
                    self.assertEqual(r[key], row[key])
                restored = e.unmask_arguments('searchMessages', {'query': 'from:header_1'})
                self.assertEqual(restored, {'query': 'from:' + row['author']})

    def test_oversized_response_withheld_entirely(self):
        e = make_engine()
        secret = 'UNSEEN-SYNTHETIC-NAME'
        result = envelope({'author': secret, 'body': 'x' * (DEFAULT_MAX_PARSE_LEN + 1)})
        result['structuredContent'] = {'author': secret}
        result['content'].append({'type': 'image', 'data': 'synthetic-image', 'mimeType': 'image/png'})
        result['extra'] = secret
        masked = e.mask_response('getMessage', result)
        self.assertTrue(masked.get('isError') is True)
        self.assertTrue(secret not in json.dumps(masked))
        self.assertTrue('structuredContent' not in masked)
        self.assertTrue('extra' not in masked)
        self.assertEqual(len(masked['content']), 1)
        self.assertEqual(e.pending_writes_count, 0)

    def test_exact_limit_allowed_and_one_character_over_blocked(self):
        text = json.dumps({'author': 'Alice'})
        with patch.dict(os.environ, {'OPENMASKIT_MAX_PARSE_BYTES': str(len(text))}):
            e = make_engine()
            good = e.mask_response('getMessage', {'content': [{'type': 'text', 'text': text}]})
            self.assertEqual(payload(good)['author'], 'header_1')
            bad = e.mask_response('getMessage', {'content': [{'type': 'text', 'text': text + ' '}]})
            self.assertTrue(bad.get('isError') is True)

    def test_large_response_without_applicable_protection_is_unchanged(self):
        e = make_engine([MaskingRule('differentTool', 'author', 'header')])
        data = envelope({'author': 'Alice', 'body': 'x' * 300})
        with patch.dict(os.environ, {'OPENMASKIT_MAX_PARSE_BYTES': '100'}):
            self.assertEqual(e.mask_response('getMessage', copy.deepcopy(data)), data)

    def test_inactive_rules_do_not_change_other_tools(self):
        e = make_engine([MaskingRule('*', 'author', 'header', active=False)])
        data = envelope({'author': 'Alice', 'body': 'x' * 300})
        with patch.dict(os.environ, {'OPENMASKIT_MAX_PARSE_BYTES': '100'}):
            self.assertEqual(e.mask_response('getMessage', copy.deepcopy(data)), data)

    def test_strip_rule_also_blocks_oversized_text(self):
        e = make_engine([MaskingRule('*', 'author', action='strip')])
        with patch.dict(os.environ, {'OPENMASKIT_MAX_PARSE_BYTES': '100'}):
            result = e.mask_response('getMessage', envelope({'author': 'Alice', 'body': 'x' * 300}))
            self.assertTrue(result.get('isError') is True)

    def test_json_field_mapper_also_blocks_oversized_text(self):
        e = make_engine([])
        e.add_mapper(ResponseMapper('*', 'json_field_mask', 'author', 'header', id=1))
        with patch.dict(os.environ, {'OPENMASKIT_MAX_PARSE_BYTES': '100'}):
            result = e.mask_response('getMessage', envelope({'author': 'Alice', 'body': 'x' * 300}))
            self.assertTrue(result.get('isError') is True)

    def test_regex_only_mapper_still_handles_large_text(self):
        e = make_engine([])
        e.add_mapper(ResponseMapper('*', 'regex_replace', 'alice@example.test', 'email', id=1))
        text = 'alice@example.test ' + 'x' * 300
        with patch.dict(os.environ, {'OPENMASKIT_MAX_PARSE_BYTES': '100'}):
            result = e.mask_response('getMessage', {'content': [{'type': 'text', 'text': text}]})
            self.assertTrue('alice@example.test' not in result['content'][0]['text'])
            self.assertTrue(result['content'][0]['text'].startswith('email_1 '))
            self.assertFalse(result.get('isError', False))

    def test_multi_block_responses_are_masked_at_each_block(self):
        text = json.dumps({'author': 'Alice'})
        data = {'content': [{'type': 'text', 'text': text}, {'type': 'text', 'text': text}]}
        with patch.dict(os.environ, {'OPENMASKIT_MAX_PARSE_BYTES': str(len(text))}):
            result = make_engine().mask_response('getMessage', data)
            self.assertTrue(all(json.loads(b['text'])['author'] == 'header_1' for b in result['content']))

    def test_parse_memory_limit_is_preserved(self):
        with patch.dict(os.environ, {'OPENMASKIT_MAX_PARSE_BYTES': '100'}):
            self.assertIsNone(try_parse_structured(json.dumps({'body': 'x' * 300})))

class PersistenceRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_alias_survives_reload_and_existing_empty_alias_is_safe(self):
        store = await MaskingStore.create(':memory:')
        try:
            await store.persist_alias('default', 'header_1', '', 'getMessage', 'ccList')
            e = MaskingEngine([MaskingRule('*', p, 'header') for p in PATHS], store)
            await e.load_aliases()
            result = payload(e.mask_response('getMessage', envelope({'author': 'Alice', 'ccList': ''})))
            self.assertEqual(result, {'author': 'header_2', 'ccList': ''})
            await e.flush_pending()
            reloaded = MaskingEngine(e.rules, store)
            await reloaded.load_aliases()
            self.assertEqual(reloaded.unmask_arguments('sendMail', {'to': 'header_2'}), {'to': 'Alice'})
            plain = reloaded.mask_response('getMessage', {'content': [{'type': 'text', 'text': 'Alice called.'}]})
            self.assertTrue(plain['content'][0]['text'] == 'header_2 called.')
        finally:
            await store.close()

if __name__ == '__main__':
    unittest.main(verbosity=2)
