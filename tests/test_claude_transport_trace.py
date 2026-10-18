"""BW-CLAUDE-TRANSPORT-R2-20261018-S1 离线回归：Claude 传输阶段被动观测。

只验证 `scripts/claude_transport_trace.cjs` 的观测契约：未启用零副作用、显式启用独占
创建、request 生命周期与同 socket 真实复用、连接错误不凭空绑定 request、错误码白名单、
HTTP 状态只记数字不假分类、任何 headers/body/query/message/stack 与嵌套密钥标记不落盘、
其他域/路径过滤、合法 JSON 行与真 UTC、既有文件不覆盖、cap 停止不抛、callback 不改请求对象。

驱动方式：临时目录里的合成 driver 通过 `diagnostics_channel.publish` 发布**合成**事件，
不建立真实连接、不访问网络/凭据/真实 API。Node 不在 PATH 时整体 skip（与本仓库既有
Node 相关离线测试一致）；离线 publish 不等于真实网络观察。
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'scripts' / 'claude_transport_trace.cjs'
NODE = shutil.which('node')

SECRET = 'SYNTHETIC_SECRET_MARKER_9f3a'
ALLOWED_ENDPOINTS = {'/anthropic/v1/messages', '/anthropic/v1/messages/count_tokens'}
ALL_CHANNELS = [
    'undici:request:create',
    'undici:request:bodySent',
    'undici:request:headers',
    'undici:request:trailers',
    'undici:request:error',
    'undici:client:beforeConnect',
    'undici:client:connected',
    'undici:client:connectError',
    'undici:client:sendHeaders',
]

DRIVER = r"""'use strict';
const dc = require('diagnostics_channel');

const scenario = process.argv[2] || '';
const out = process.argv[3] || '';
const modPath = process.argv[4] || '';
const SECRET = 'SYNTHETIC_SECRET_MARKER_9f3a';

let mod = null;
if (scenario !== 'preload') mod = require(modPath);

const CH = {
  create: 'undici:request:create',
  bodySent: 'undici:request:bodySent',
  headers: 'undici:request:headers',
  trailers: 'undici:request:trailers',
  error: 'undici:request:error',
  beforeConnect: 'undici:client:beforeConnect',
  connected: 'undici:client:connected',
  connectError: 'undici:client:connectError',
  sendHeaders: 'undici:client:sendHeaders',
};
const ALL = Object.values(CH);
const DEEPSEEK = { hostname: 'api.deepseek.com', port: 443, protocol: 'https:' };

function report(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }
function subs() { const o = {}; for (const c of ALL) o[c] = dc.hasSubscribers(c); return o; }
function pub(channel, payload) { dc.channel(channel).publish(payload); }

function makeRequest(origin, path, method) {
  return {
    origin: origin,
    path: path,
    method: method,
    headers: { authorization: 'Bearer ' + SECRET, cookie: 'sid=' + SECRET },
    body: 'body-' + SECRET,
  };
}

function lifecycle() {
  const socket = { tag: 'socket-A' };
  const first = makeRequest(new URL('https://api.deepseek.com'),
    '/anthropic/v1/messages?beta=' + SECRET, 'POST');
  pub(CH.create, { request: first });
  pub(CH.bodySent, { request: first });
  pub(CH.sendHeaders, { request: first, socket: socket });
  pub(CH.headers, { request: first, response: { statusCode: 200, headers: { 'set-cookie': SECRET } } });
  pub(CH.trailers, { request: first, trailers: { 'x-secret': SECRET } });
  const second = makeRequest(new URL('https://api.deepseek.com'),
    '/anthropic/v1/messages/count_tokens', 'POST');
  pub(CH.create, { request: second });
  pub(CH.sendHeaders, { request: second, socket: socket });
  pub(CH.headers, { request: second, response: { statusCode: 429 } });
  pub(CH.beforeConnect, { connectParams: DEEPSEEK });
  pub(CH.connected, { connectParams: DEEPSEEK, socket: socket });
}

function deepError() {
  const c4 = Object.assign(new Error('l4 ' + SECRET), { code: 'ECONNREFUSED' });
  const c3 = Object.assign(new Error('l3 ' + SECRET), { code: 'ETIMEDOUT', cause: c4 });
  const c2 = Object.assign(new Error('l2 ' + SECRET), { code: 'UND_ERR_SOCKET', cause: c3 });
  const c1 = Object.assign(new Error('l1 ' + SECRET), { code: 'EPIPE', syscall: 'write', cause: c2 });
  return Object.assign(new Error('outer ' + SECRET),
    { code: 'ECONNRESET', syscall: 'read', cause: c1 });
}

function connectScenario() {
  pub(CH.beforeConnect, { connectParams: DEEPSEEK });
  const err = deepError();
  pub(CH.connectError, { connectParams: DEEPSEEK, error: err });
  const unknown = Object.assign(new Error('unknown ' + SECRET),
    { code: 'EACCES', syscall: 'open', cause: { message: SECRET } });
  pub(CH.connectError, { connectParams: DEEPSEEK, error: unknown });
  const request = makeRequest(new URL('https://api.deepseek.com'), '/anthropic/v1/messages', 'POST');
  pub(CH.create, { request: request });
  pub(CH.error, { request: request, error: err });
  return { safe: mod.safeError(err) };
}

function statusScenario() {
  for (const code of [401, 402, 429]) {
    const request = makeRequest(new URL('https://api.deepseek.com'), '/anthropic/v1/messages', 'POST');
    pub(CH.create, { request: request });
    pub(CH.sendHeaders, { request: request, socket: { tag: 's-' + code } });
    pub(CH.headers, {
      request: request,
      response: { statusCode: code, headers: { authorization: SECRET, cookie: SECRET } },
    });
  }
}

function secretsScenario() {
  const request = makeRequest('https://api.deepseek.com?token=' + SECRET,
    '/anthropic/v1/messages?api_key=' + SECRET, 'POST');
  pub(CH.create, { request: request });
  pub(CH.sendHeaders, { request: request, socket: { tag: SECRET } });
  pub(CH.headers, {
    request: request,
    response: { statusCode: 200, headers: { authorization: SECRET, cookie: SECRET }, body: SECRET },
  });
  pub(CH.trailers, { request: request, trailers: { 'x-secret': SECRET } });
  const err = Object.assign(new Error('message ' + SECRET), {
    code: 'ECONNRESET', stack: 'stack ' + SECRET,
    headers: { authorization: SECRET }, body: SECRET, detail: { deep: SECRET },
    cause: Object.assign(new Error('cause ' + SECRET), { code: 'EPIPE', message: SECRET }),
  });
  pub(CH.error, { request: request, error: err });
}

function filterScenario() {
  const cases = [
    ['https://api.openai.com', '/anthropic/v1/messages'],
    ['http://api.deepseek.com', '/anthropic/v1/messages'],
    ['https://api.deepseek.com:8443', '/anthropic/v1/messages'],
    ['https://api.deepseek.com', '/v1/chat/completions'],
    ['https://api.deepseek.com', '/anthropic/v1/messages/../messages'],
    ['https://api.deepseek.com.evil.test', '/anthropic/v1/messages'],
  ];
  for (const pair of cases) {
    const request = makeRequest(pair[0], pair[1], 'POST');
    pub(CH.create, { request: request });
    pub(CH.bodySent, { request: request });
    pub(CH.sendHeaders, { request: request, socket: { tag: pair[0] } });
    pub(CH.headers, { request: request, response: { statusCode: 200 } });
  }
  const foreign = { hostname: 'api.openai.com', port: 443, protocol: 'https:' };
  pub(CH.beforeConnect, { connectParams: foreign });
  pub(CH.connected, { connectParams: foreign, socket: { tag: 'x' } });
  pub(CH.connectError, {
    connectParams: foreign,
    error: Object.assign(new Error('x'), { code: 'ECONNRESET' }),
  });
}

function frozenScenario() {
  const request = Object.freeze(makeRequest(new URL('https://api.deepseek.com'),
    '/anthropic/v1/messages', 'POST'));
  const payload = Object.freeze({ request: request });
  const before = JSON.stringify({
    o: String(request.origin), p: request.path, m: request.method, k: Object.keys(request).sort(),
  });
  pub(CH.create, payload);
  pub(CH.bodySent, payload);
  pub(CH.sendHeaders, Object.freeze({ request: request, socket: Object.freeze({ tag: 'frozen' }) }));
  pub(CH.headers, Object.freeze({ request: request, response: Object.freeze({ statusCode: 200 }) }));
  const after = JSON.stringify({
    o: String(request.origin), p: request.path, m: request.method, k: Object.keys(request).sort(),
  });
  return { same: before === after, keys: Object.keys(request).sort() };
}

if (scenario === 'disabled') {
  report({ subs: subs(), defaultRecorder: mod.getDefaultRecorder() === null });
} else if (scenario === 'env') {
  lifecycle();
  const r = mod.getDefaultRecorder();
  report({ enabled: !!(r && r.enabled), stats: r ? r.stats() : null, subs: subs() });
} else if (scenario === 'preload') {
  lifecycle();
  report({ subs: subs() });
} else if (scenario === 'lifecycle') {
  const r = mod.createRecorder({ filePath: out });
  lifecycle();
  const before = subs();
  const statsBefore = r.stats();
  r.close();
  report({ enabled: r.enabled, stats: statsBefore, subs: before, subs_after_close: subs() });
} else if (scenario === 'connect') {
  const r = mod.createRecorder({ filePath: out });
  const extra = connectScenario();
  report({ enabled: r.enabled, stats: r.stats(), safe: extra.safe });
  r.close();
} else if (scenario === 'status') {
  const r = mod.createRecorder({ filePath: out });
  statusScenario();
  report({ enabled: r.enabled, stats: r.stats() });
  r.close();
} else if (scenario === 'secrets') {
  const r = mod.createRecorder({ filePath: out });
  secretsScenario();
  report({ enabled: r.enabled, stats: r.stats() });
  r.close();
} else if (scenario === 'filter') {
  const r = mod.createRecorder({ filePath: out });
  filterScenario();
  report({ enabled: r.enabled, stats: r.stats() });
  r.close();
} else if (scenario === 'frozen') {
  const r = mod.createRecorder({ filePath: out });
  const extra = frozenScenario();
  report({ enabled: r.enabled, stats: r.stats(), same: extra.same, keys: extra.keys });
  r.close();
} else if (scenario === 'cap') {
  const r = mod.createRecorder({ filePath: out, maxRecords: 3 });
  for (let i = 0; i < 6; i += 1) {
    const request = makeRequest(new URL('https://api.deepseek.com'),
      '/anthropic/v1/messages', 'POST');
    pub(CH.create, { request: request });
  }
  report({ enabled: r.enabled, stats: r.stats() });
  r.close();
} else if (scenario === 'bytes') {
  const r = mod.createRecorder({ filePath: out, maxBytes: 220 });
  for (let i = 0; i < 6; i += 1) {
    const request = makeRequest(new URL('https://api.deepseek.com'),
      '/anthropic/v1/messages', 'POST');
    pub(CH.create, { request: request });
  }
  report({ enabled: r.enabled, stats: r.stats() });
  r.close();
} else if (scenario === 'open') {
  const r = mod.createRecorder({ filePath: out });
  report({ enabled: r.enabled, stats: r.stats() });
  r.close();
} else {
  throw new Error('unknown scenario ' + scenario);
}
"""


@unittest.skipUnless(NODE, 'node 不在 PATH，跳过 Node 观测模块的离线验证')
class TransportTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.driver = self.base / 'driver.cjs'
        self.driver.write_text(DRIVER, encoding='utf-8')
        self.addCleanup(self.tmp.cleanup)

    def run_driver(self, scenario, trace_path='', *, env_extra=None, args=()):
        env = {k: v for k, v in os.environ.items()
               if k not in ('BRAIN_WORKER_CLAUDE_TRACE_FILE', 'NODE_OPTIONS')}
        if env_extra:
            env.update(env_extra)
        cmd = [NODE] + list(args) + [str(self.driver), scenario,
                                     str(trace_path), str(MODULE)]
        proc = subprocess.run(cmd, cwd=str(self.base), env=env, capture_output=True,
                              timeout=60)
        stdout = proc.stdout.decode('utf-8', 'replace')
        stderr = proc.stderr.decode('utf-8', 'replace')
        self.assertEqual(proc.returncode, 0, (stdout, stderr))
        lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, (stdout, stderr))
        return json.loads(lines[0]), stderr

    def read_records(self, path):
        text = Path(path).read_text(encoding='utf-8')
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def assert_no_secret(self, path):
        text = Path(path).read_text(encoding='utf-8')
        self.assertNotIn(SECRET, text)
        self.assertNotIn('Bearer', text)
        self.assertNotIn('set-cookie', text)
        self.assertNotIn('api_key', text)
        self.assertNotIn('authorization', text)
        self.assertNotIn('cookie', text)

    def test_disabled_without_env_zero_file_zero_subscription(self):
        report, stderr = self.run_driver('disabled')
        self.assertEqual(stderr, '')
        self.assertIs(report['defaultRecorder'], True)
        self.assertEqual(report['subs'], {name: False for name in ALL_CHANNELS})
        leftovers = [p.name for p in self.base.iterdir() if p.suffix == '.jsonl']
        self.assertEqual(leftovers, [])

    def test_env_enable_creates_file_and_full_lifecycle(self):
        trace = self.base / 'trace.jsonl'
        report, stderr = self.run_driver('env',
                                         env_extra={'BRAIN_WORKER_CLAUDE_TRACE_FILE': str(trace)})
        self.assertEqual(stderr, '')
        self.assertTrue(report['enabled'])
        self.assertEqual(report['subs'], {name: True for name in ALL_CHANNELS})
        self.assertTrue(trace.exists())
        records = self.read_records(trace)
        self.assertEqual([r['event'] for r in records], [
            'request:create', 'request:bodySent', 'client:sendHeaders',
            'request:headers', 'request:trailers',
            'request:create', 'client:sendHeaders', 'request:headers',
            'client:beforeConnect', 'client:connected',
        ])
        creates = [r for r in records if r['event'] == 'request:create']
        self.assertEqual([r['request_id'] for r in creates], [1, 2])
        self.assertEqual([r['method'] for r in creates], ['POST', 'POST'])
        self.assertEqual([r['endpoint'] for r in creates],
                         ['/anthropic/v1/messages', '/anthropic/v1/messages/count_tokens'])
        sends = [r for r in records if r['event'] == 'client:sendHeaders']
        self.assertEqual([r['request_id'] for r in sends], [1, 2])
        self.assertEqual([r['socket_id'] for r in sends], [1, 1])
        self.assertEqual([r['socket_uses'] for r in sends], [1, 2])
        self.assertEqual([r['socket_reused'] for r in sends], [False, True])
        headers = [r for r in records if r['event'] == 'request:headers']
        self.assertEqual([r['status'] for r in headers], [200, 429])
        connected = [r for r in records if r['event'] == 'client:connected']
        self.assertEqual(connected[0]['socket_id'], 1)
        self.assertNotIn('request_id', connected[0])
        self.assert_no_secret(trace)

    def test_explicit_create_recorder_and_close_unsubscribes(self):
        trace = self.base / 'lifecycle.jsonl'
        report, _ = self.run_driver('lifecycle', trace)
        self.assertTrue(report['enabled'])
        self.assertEqual(report['stats']['records'], 10)
        self.assertEqual(report['subs'], {name: True for name in ALL_CHANNELS})
        self.assertEqual(report['subs_after_close'],
                         {name: False for name in ALL_CHANNELS})
        self.assertEqual(len(self.read_records(trace)), 10)

    def test_connect_events_not_bound_to_any_request(self):
        trace = self.base / 'connect.jsonl'
        report, _ = self.run_driver('connect', trace)
        self.assertTrue(report['enabled'])
        records = self.read_records(trace)
        before = [r for r in records if r['event'] == 'client:beforeConnect']
        self.assertEqual(len(before), 1)
        self.assertEqual(sorted(before[0]), ['event', 'ms', 't'])
        errors = [r for r in records if r['event'] == 'client:connectError']
        self.assertEqual(len(errors), 2)
        for rec in errors:
            self.assertNotIn('request_id', rec)
            self.assertNotIn('socket_id', rec)
        first = errors[0]['error']
        self.assertEqual(first['code'], 'ECONNRESET')
        self.assertEqual(first['syscall'], 'read')
        self.assertEqual(len(first['cause']), 3)
        self.assertEqual([c.get('code') for c in first['cause']],
                         ['EPIPE', 'UND_ERR_SOCKET', 'ETIMEDOUT'])
        self.assertEqual(first['cause'][0].get('syscall'), 'write')
        self.assertEqual(report['safe'], first)
        second = errors[1]['error']
        self.assertEqual(second['code'], 'unknown')
        self.assertNotIn('syscall', second)
        req_err = [r for r in records if r['event'] == 'request:error']
        self.assertEqual(len(req_err), 1)
        self.assertEqual(req_err[0]['request_id'], 1)
        self.assertEqual(req_err[0]['error']['code'], 'ECONNRESET')
        self.assert_no_secret(trace)

    def test_http_status_recorded_without_recovery_classification(self):
        trace = self.base / 'status.jsonl'
        self.run_driver('status', trace)
        records = self.read_records(trace)
        headers = [r for r in records if r['event'] == 'request:headers']
        self.assertEqual([r['status'] for r in headers], [401, 402, 429])
        for rec in headers:
            self.assertEqual(sorted(rec), ['event', 'ms', 'request_id', 'status', 't'])
        text = Path(trace).read_text(encoding='utf-8')
        for word in ('retry', 'recover', 'quota', 'health', 'transport', 'success'):
            self.assertNotIn(word, text)

    def test_headers_body_query_message_stack_never_written(self):
        trace = self.base / 'secrets.jsonl'
        self.run_driver('secrets', trace)
        records = self.read_records(trace)
        self.assertGreaterEqual(len(records), 5)
        self.assert_no_secret(trace)
        text = Path(trace).read_text(encoding='utf-8')
        self.assertNotIn('"stack"', text)
        self.assertNotIn('"message"', text)
        self.assertNotIn('"detail"', text)
        create = [r for r in records if r['event'] == 'request:create'][0]
        self.assertEqual(create['endpoint'], '/anthropic/v1/messages')
        self.assertNotIn('?', create['endpoint'])

    def test_other_hosts_and_paths_filtered(self):
        trace = self.base / 'filter.jsonl'
        report, _ = self.run_driver('filter', trace)
        self.assertTrue(report['enabled'])
        self.assertEqual(report['stats']['records'], 0)
        self.assertEqual(trace.stat().st_size, 0)

    def test_records_are_json_lines_with_real_utc(self):
        trace = self.base / 'utc.jsonl'
        self.run_driver('env', env_extra={'BRAIN_WORKER_CLAUDE_TRACE_FILE': str(trace)})
        text = trace.read_text(encoding='utf-8')
        for line in text.splitlines():
            rec = json.loads(line)
            self.assertTrue(rec['t'].endswith('Z'))
            parsed = datetime.fromisoformat(rec['t'].replace('Z', '+00:00'))
            self.assertEqual(parsed.tzinfo, timezone.utc)
            self.assertIsInstance(rec['ms'], int)
            self.assertGreaterEqual(rec['ms'], 0)

    def test_existing_file_not_overwritten(self):
        trace = self.base / 'existing.jsonl'
        trace.write_text('PRESERVE-ME\n', encoding='utf-8')
        report, _ = self.run_driver('open', trace)
        self.assertFalse(report['enabled'])
        self.assertEqual(trace.read_text(encoding='utf-8'), 'PRESERVE-ME\n')

    def test_missing_parent_and_relative_path_disabled(self):
        missing = self.base / 'no-such-dir' / 'trace.jsonl'
        report, _ = self.run_driver('open', missing)
        self.assertFalse(report['enabled'])
        self.assertFalse(missing.parent.exists())
        report2, _ = self.run_driver('open', 'relative-trace.jsonl')
        self.assertFalse(report2['enabled'])
        self.assertFalse((self.base / 'relative-trace.jsonl').exists())

    def test_record_cap_stops_without_throwing(self):
        trace = self.base / 'cap.jsonl'
        report, _ = self.run_driver('cap', trace)
        self.assertTrue(report['enabled'])
        self.assertEqual(report['stats']['records'], 3)
        self.assertIs(report['stats']['stopped'], True)
        self.assertEqual(len(self.read_records(trace)), 3)

    def test_byte_cap_stops_without_throwing(self):
        trace = self.base / 'bytes.jsonl'
        report, _ = self.run_driver('bytes', trace)
        self.assertTrue(report['enabled'])
        self.assertGreater(trace.stat().st_size, 0)
        self.assertLessEqual(trace.stat().st_size, 220)
        self.assertLessEqual(len(self.read_records(trace)), 2)

    def test_callbacks_do_not_mutate_request_object(self):
        trace = self.base / 'frozen.jsonl'
        report, _ = self.run_driver('frozen', trace)
        self.assertTrue(report['same'])
        self.assertEqual(report['keys'], ['body', 'headers', 'method', 'origin', 'path'])
        self.assertEqual(report['stats']['records'], 4)

    def test_child_only_preload_via_node_options(self):
        trace = self.base / 'preload.jsonl'
        node_options = '--require ' + str(MODULE).replace('\\', '/')
        report, stderr = self.run_driver(
            'preload', trace,
            env_extra={'BRAIN_WORKER_CLAUDE_TRACE_FILE': str(trace),
                       'NODE_OPTIONS': node_options})
        self.assertEqual(stderr, '')
        self.assertEqual(report['subs'], {name: True for name in ALL_CHANNELS})
        records = self.read_records(trace)
        self.assertGreaterEqual(len(records), 8)
        self.assert_no_secret(trace)


if __name__ == '__main__':
    unittest.main()
