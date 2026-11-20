"""Offline checks against the pinned real iCode runtime and frozen R5.

Run as: PYTHONPATH=<adapter>:<frozen>/src uv run --no-sync --project <iCode> python test_r5_native.py
No credentials or external model endpoint are used.
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    import r5_native as r
except ModuleNotFoundError as exc:
    if exc.name not in {'r5_native', 'chrys'}:
        raise
    raise unittest.SkipTest('Requires the pinned iCode/R5 integration environment') from exc
from chrys.kernel._types import Message, ChatResponse
from chrys.kernel._content import Content
from chrys.kernel.middleware import ChatContext, ChatMiddlewarePipeline, FunctionInvocationContext
from chrys.service.agent_middleware.injection import InjectionMiddleware
from compass_brief.ports import ModelReply

ROOT = Path(r.__file__).resolve().parent

class FakeMaintenance:
    async def complete(self, *, response_schema, **kwargs):
        if response_schema['title'] == 'IssueRequirementExtraction':
            data = {'requirements': [{'id': 'R1', 'text': 'Return an empty list for empty input.'}],
                    'constraints': [], 'signature_changes': []}
        else:
            data = {k: [] for k in ('new_claims', 'augment', 'merge', 'conflicts', 'contradiction_scan', 'deactivate')}
        return ModelReply(json.dumps(data))

class AdapterChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name).resolve()
        self.constants = r.frozen_constants(ROOT / 'r5-frozen')
        self.c = r.Controller('Return an empty list for empty input.', self.directory, self.directory,
                              self.constants, FakeMaintenance())
        r.controller = self.c
        self.c.tool_kinds = {'read_file': 'filesystem.read', 'write_file': 'filesystem.write', 'execute': 'shell'}

    def tearDown(self):
        self.tmp.cleanup()

    def observation(self, identity='read1', name='read_file', args=None, meta=None):
        return [Message('assistant', [Content.from_function_call(identity, name, arguments=args or {'path': 'a.py'})]),
                Message('tool', [Content.from_function_result(identity, result='visible output',
                    additional_properties={r.TOOL_RESULT_METADATA_KEY: meta or {}})])]

    async def test_native_injection_handoff_and_evidence(self):
        await self.c.capture(self.observation(), maintain=False)
        self.c.batch_size = 2
        self.assertIn('alone', await self.c.handoff('looked at a.py'))
        self.assertFalse(self.c.started)
        self.c.batch_size = 1
        self.assertIn('started', await self.c.handoff('looked at a.py'))
        injection = InjectionMiddleware()
        chain = ChatMiddlewarePipeline(r.BriefChat(self.c, injection), injection)
        sent = []
        async def final(ctx):
            sent.append(ctx.messages)
            return ChatResponse(messages=[Message('assistant', ['I read the Brief.'])], model=r.MODEL)
        await chain.execute(ChatContext(None, [Message('user', ['Continue'])], {}, stream=False), final)
        self.assertTrue(self.c.handoff_seen)
        self.assertIn(self.constants['SUBMISSION_RULES'], '\n'.join(m.text for m in sent[0]))
        await chain.execute(ChatContext(None, [Message('user', ['Continue'])], {}, stream=False), final)
        self.assertNotIn(self.constants['SUBMISSION_RULES'], '\n'.join(m.text for m in sent[1]))
        self.assertEqual(self.c.calls, 2)
        # Neither self-authored text nor our own details/denials become repository evidence.
        await self.c.capture(self.observation(), maintain=False)
        await self.c.capture(self.observation('blocked', 'write_file', meta={'compass_control': True}), maintain=False)
        await self.c.capture(self.observation('reject', meta={'approval': 'user_rejected'}), maintain=False)
        self.assertEqual(len(self.c.core.evidence.snapshot()), 1)
        await self.c.capture(self.observation('shell1', 'execute', {'command': 'false'}, {'shell_exit_code': 1}), maintain=False)
        self.assertEqual(self.c.core.evidence.snapshot()[-1].exit_code, 1)
        self.assertEqual(self.c.core.evidence.snapshot()[-1].outcome, 'error')

    async def test_write_guard_and_call_budget(self):
        ctx = FunctionInvocationContext(SimpleNamespace(name='write_file', chrys_kind='filesystem.write'), {'path': 'a.py'})
        call = AsyncMock()
        guard = r.ExplorationGuard(self.c)
        await guard.process(ctx, call)
        call.assert_not_awaited()
        self.assertTrue(ctx.metadata[r.TOOL_RESULT_METADATA_KEY]['compass_control'])
        self.c.handoff_seen = True
        await guard.process(ctx, call)
        call.assert_awaited_once()
        self.c.calls = r.MAX_CALLS
        with self.assertRaisesRegex(RuntimeError, 'budget'):
            await r.BriefChat(self.c, InjectionMiddleware()).process(ChatContext(None, [], {}), call)

    async def test_double_request_observation_counts_once_at_budget_boundary(self):
        self.c.calls = r.MAX_CALLS - 1
        invocations = []
        chain = ChatMiddlewarePipeline(r.BriefChat(self.c, InjectionMiddleware()))
        async def provider(ctx):
            # The pipeline already notified; the real client notifies again
            # after preparing provider messages. Both belong to this call.
            for observer in ctx.request_message_observers:
                observer(ctx.messages)
            invocations.append(True)
            return ChatResponse(messages=[Message('assistant', ['done'])], model=r.MODEL)
        await chain.execute(ChatContext(None, [Message('user', ['continue'])], {}, stream=False), provider)
        self.assertEqual(self.c.calls, r.MAX_CALLS)
        self.assertEqual(len(invocations), 1)
        with self.assertRaisesRegex(RuntimeError, 'budget'):
            await chain.execute(ChatContext(None, [], {}, stream=False), provider)
        self.assertEqual(len(invocations), 1)
        events = [json.loads(line) for line in (self.directory / 'adapter-events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(e['event'] == 'main_request' for e in events), 1)
        self.assertTrue(any(e['event'] == 'budget_exhausted' for e in events))

    async def test_final_continuation_and_error_boundary(self):
        from chrys.orchestration.engine.build import builder
        from chrys.orchestration.session_host import ChrysSessionHost
        for calls, fail, expected in [(10, False, 2), (236, False, 1), (10, True, 1)]:
            c = r.Controller('test', self.directory, self.directory, self.constants)
            c.started, c.rules_seen, c.calls = True, True, calls
            c.core.maintain = AsyncMock()
            original = AsyncMock(side_effect=RuntimeError('synthetic') if fail else None,
                                 return_value=SimpleNamespace(text='done', session_id='same'))
            with patch.object(ChrysSessionHost, 'run_until_final', original), patch.object(builder, 'create_runtime'):
                r.install_runtime(c)
                if fail:
                    with self.assertRaisesRegex(RuntimeError, 'synthetic'):
                        await ChrysSessionHost.run_until_final(object(), 'task')
                else:
                    await ChrysSessionHost.run_until_final(object(), 'task')
                self.assertEqual(original.await_count, expected)
                if expected == 2:
                    self.assertIs(original.await_args_list[0].args[0], original.await_args_list[1].args[0])
                    self.assertIn(self.constants['SUBMISSION_CHECK'], original.await_args_list[1].args[1])

    async def test_no_brief_is_not_a_valid_r5_run(self):
        from chrys.orchestration.engine.build import builder
        from chrys.orchestration.session_host import ChrysSessionHost
        original = AsyncMock(return_value=SimpleNamespace(text='done'))
        self.c.model = None
        with patch.object(ChrysSessionHost, 'run_until_final', original), patch.object(builder, 'create_runtime'):
            r.install_runtime(self.c)
            with self.assertRaisesRegex(RuntimeError, 'not consumed'):
                await ChrysSessionHost.run_until_final(object(), 'task')
        original.assert_awaited_once()

    async def test_official_maintenance_wire_is_offline(self):
        import httpx
        from openai import AsyncOpenAI
        bodies = []
        async def handler(request):
            self.assertEqual(request.url.host, 'openrouter.ai')
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={'id':'synthetic', 'object':'chat.completion', 'created':0,
                'model':r.MODEL, 'choices':[{'index':0,'message':{'role':'assistant','content':'{}'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3}})
        model = r.OpenRouterMaintenance('offline-placeholder', self.c.audit)
        await model.client.close()
        model.client = AsyncOpenAI(api_key='offline-placeholder', base_url=r.BASE_URL, max_retries=0,
                                  http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            await model.complete(system_prompt='system', user_prompt='user', response_schema={'type':'object'},
                                 max_output_tokens=100, timeout_seconds=5)
        finally:
            await model.client.close()
        self.assertEqual(bodies[0]['reasoning'], {'enabled':False})
        self.assertEqual(bodies[0]['response_format'], {'type':'json_object'})
        self.assertEqual(bodies[0]['provider'], r.PROVIDER)

    async def test_config_and_key_file(self):
        config = r.configure(self.directory, ROOT / 'iCode')
        r.bind_config(config)
        from chrys.service.profiles.agents.registry import AgentProfileRegistry
        from chrys.service.profiles.models.registry import ModelProfileRegistry
        agents, models = AgentProfileRegistry(), ModelProfileRegistry()
        self.assertEqual(agents.load_user_profiles(config / 'agents'), 1)
        self.assertEqual(models.load_profiles(config / 'models'), 1)
        self.assertIsNotNone(agents.get('R5Code'))
        key = self.directory / 'synthetic.key'
        r.Artifacts.write_text(key, 'offline-placeholder\n')
        self.assertEqual(r.read_key(key), 'offline-placeholder')
        key.chmod(0o644)
        with self.assertRaises(PermissionError): r.read_key(key)

if __name__ == '__main__':
    unittest.main(verbosity=2)
