"""Offline regression checks for the actual serialized request guard."""
import copy
import json
import unittest
import httpx
from wire_audit import MODEL, PROVIDER, validate_request


class WireChecks(unittest.TestCase):
    def setUp(self):
        self.body = {'model': MODEL, 'provider': PROVIDER, 'reasoning': {'effort': 'max'},
                     'max_tokens': 64000, 'tools': [{'type': 'function'}], 'stream': False}

    def check(self, body, url='https://openrouter.ai/api/v1/chat/completions'):
        return validate_request(httpx.Request('POST', url, json=body))

    def test_valid_main_and_maintenance(self):
        role, evidence = self.check(self.body)
        self.assertEqual(role, 'main')
        self.assertEqual(evidence['reasoning'], {'effort': 'max'})
        body = {**self.body, 'reasoning': {'enabled': False}, 'temperature': 0, 'max_tokens': 16384}
        body.pop('tools')
        self.assertEqual(self.check(body)[0], 'maintenance')

    def test_bad_request_fails_before_network(self):
        changes = [dict(reasoning={'effort': 'high'}), dict(max_tokens=32000),
                   dict(provider={'only': ['deepinfra']}), dict(model='other'), dict(stream=True),
                   dict(reasoning_effort='max'), dict(thinking={'type': 'enabled'})]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.check({**self.body, **change})
        with self.assertRaises(RuntimeError):
            self.check(self.body, 'https://other.example/chat/completions')

    def test_evidence_excludes_content(self):
        body = {**self.body, 'messages': [{'role': 'user', 'content': 'private synthetic text'}]}
        _, evidence = self.check(body)
        self.assertNotIn('private synthetic text', json.dumps(evidence))
        self.assertNotIn('tools', evidence)


if __name__ == '__main__':
    unittest.main(verbosity=2)
