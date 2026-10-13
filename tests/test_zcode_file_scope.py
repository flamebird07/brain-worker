"""BW-ZCODE-FILES-S1 逐文件读写边界离线回归（不登录、不联网、不调真实模型）。

覆盖三层，全部离线、只用临时目录与合成文件：
  1. Python 侧派工前规范化/冻结/覆盖校验（scripts/zcode_file_scope.py 纯逻辑）；
  2. 跨语言 canonical SHA 同源（Python 冻结 ↔ JS 重算，同一规范化口径）；
  3. JS 受控 FileSystemPort 行为（直接驱动真实 scripts/zcode_file_scope.mjs 的
     buildFileScopeGuard + createGuardedFileSystemPort 面对真实临时工作区与合成 inner 端口）：
     放行登记读写、拒绝只读参考写/未登记/越界/删除/列目录/搜索、createDirectory 只放已存在必要父目录、
     连续合法 Edit 不误判越界、强制 createParents=false 并透传 expectedRevision、对外方法面恰为官方 10 个。
绝不 mock 被测守卫本身，也不复刻生产分支；inner 端口只做调用记录与最小返回。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import zcode_file_scope as zfs  # noqa: E402

PORT_METHODS = ['createDirectory', 'stat', 'readTextFile', 'readBinaryFile',
                'readTextFileRange', 'writeTextFile', 'removeFile', 'listDirectory',
                'searchFiles', 'searchText']


def _sha_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _drive_node(driver_text, args):
    """把 driver.mjs 落到临时文件并用真实 node 执行；返回解析后的 stdout JSON。"""
    with tempfile.TemporaryDirectory() as td:
        dpath = Path(td) / 'driver.mjs'
        dpath.write_text(driver_text, encoding='utf-8')
        proc = subprocess.run([shutil.which('node'), str(dpath)] + list(args),
                              capture_output=True, timeout=60)
        assert proc.returncode == 0, proc.stderr.decode('utf-8', 'replace')
        out = proc.stdout.decode('utf-8', 'replace').strip().splitlines()[-1]
        return json.loads(out)


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / 'ws'
        self.ws.mkdir()
        self.ext = Path(self._tmp.name) / 'ext'
        self.ext.mkdir()

    def test_happy_freezes_sorted_deduped_with_sha(self):
        (self.ws / 'read.txt').write_text('R\n', encoding='utf-8')
        (self.ws / 'write.txt').write_text('W\n', encoding='utf-8')
        (self.ws / 'sub').mkdir()
        (self.ext / 'ref.txt').write_text('REF\n', encoding='utf-8')
        contract = {'workspace': str(self.ws),
                    'readable_files': ['read.txt', os.path.relpath(self.ext / 'ref.txt', self.ws)],
                    'writable_files': ['write.txt', 'sub/new.txt', 'write.txt']}
        norm, reasons = zfs.normalize_file_scope(contract, self.ws)
        self.assertEqual(reasons, [], reasons)
        self.assertEqual(norm['workspace'], zfs._normcase(os.path.realpath(self.ws)))
        # 升序去重：write.txt 重复项只留一份；集合非空。
        self.assertEqual(len(norm['writable_files']),
                         len({e['real'] for e in norm['writable_files']}))
        reals = [e['real'] for e in norm['readable_files']]
        self.assertEqual(reals, sorted(reals))
        # 已存在文件带内容 SHA；新写文件 sha256=None。
        wr = {e['real']: e['sha256'] for e in norm['writable_files']}
        self.assertTrue(any(v is None for v in wr.values()))
        self.assertTrue(any(v is not None and len(v) == 64 for v in wr.values()))

    def test_writable_outside_workspace_rejected(self):
        contract = {'workspace': str(self.ws), 'readable_files': [],
                    'writable_files': [os.path.relpath(self.ext / 'x.txt', self.ws)]}
        norm, reasons = zfs.normalize_file_scope(contract, self.ws)
        self.assertIsNone(norm)
        self.assertTrue(any('outside the workspace' in r for r in reasons), reasons)

    def test_missing_workspace_match_rejected(self):
        other = Path(self._tmp.name) / 'other'
        other.mkdir()
        contract = {'workspace': str(other), 'readable_files': [], 'writable_files': []}
        norm, reasons = zfs.normalize_file_scope(contract, self.ws)
        self.assertIsNone(norm)
        self.assertTrue(any('does not resolve to the dispatch workspace' in r for r in reasons),
                        reasons)

    def test_readable_nonexistent_rejected(self):
        contract = {'workspace': str(self.ws), 'readable_files': ['ghost.txt'],
                    'writable_files': []}
        norm, reasons = zfs.normalize_file_scope(contract, self.ws)
        self.assertIsNone(norm)
        self.assertTrue(any('readable file does not exist' in r for r in reasons), reasons)

    def test_new_file_missing_parent_dir_rejected_implicit_creation(self):
        contract = {'workspace': str(self.ws), 'readable_files': [],
                    'writable_files': ['nope/deep/new.txt']}
        norm, reasons = zfs.normalize_file_scope(contract, self.ws)
        self.assertIsNone(norm)
        self.assertTrue(any('implicit directory creation is refused' in r for r in reasons),
                        reasons)

    def test_directory_as_writable_rejected(self):
        (self.ws / 'adir').mkdir()
        contract = {'workspace': str(self.ws), 'readable_files': [],
                    'writable_files': ['adir']}
        norm, reasons = zfs.normalize_file_scope(contract, self.ws)
        self.assertIsNone(norm)
        self.assertTrue(any('directory, not a file' in r for r in reasons), reasons)

    def test_malformed_contract_rejected(self):
        for bad in ([], {'workspace': 'x'}, {'workspace': 'x', 'readable_files': 'y',
                                             'writable_files': []}):
            norm, reasons = zfs.normalize_file_scope(bad, self.ws)
            self.assertIsNone(norm, bad)
            self.assertTrue(reasons, bad)

    def test_non_string_entry_rejected(self):
        contract = {'workspace': str(self.ws), 'readable_files': ['  padded  '],
                    'writable_files': []}
        norm, reasons = zfs.normalize_file_scope(contract, self.ws)
        self.assertIsNone(norm)
        self.assertTrue(any('non-empty untrimmed' in r for r in reasons), reasons)


class PlanCoverageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / 'ws'
        self.ws.mkdir()
        (self.ws / 'a.py').write_text('A\n', encoding='utf-8')
        contract = {'workspace': str(self.ws), 'readable_files': [], 'writable_files': ['a.py']}
        self.norm, reasons = zfs.normalize_file_scope(contract, self.ws)
        self.assertEqual(reasons, [])

    def test_exact_single_file_edit_covers_writable(self):
        self.assertEqual(zfs.plan_edit_coverage_reasons(
            [f'Edit({self.norm["writable_files"][0]["real"]})'], self.norm, self.ws), [])

    def test_relative_form_matches_registered(self):
        # 计划用相对路径 Edit，规范化后应命中同一 writable real（相对/绝对一致化）。
        self.assertEqual(zfs.plan_edit_coverage_reasons(['Edit(a.py)'], self.norm, self.ws), [])

    def test_wildcard_edit_rejected(self):
        reasons = zfs.plan_edit_coverage_reasons(['Edit(*.py)'], self.norm, self.ws)
        self.assertTrue(any('wildcard' in r for r in reasons), reasons)

    def test_unverified_form_rejected(self):
        reasons = zfs.plan_edit_coverage_reasons(['Edit(a.py'], self.norm, self.ws)
        self.assertTrue(any('unsupported Edit form' in r for r in reasons), reasons)

    def test_edit_outside_workspace_rejected(self):
        reasons = zfs.plan_edit_coverage_reasons(['Edit(../outside.py)'], self.norm, self.ws)
        self.assertTrue(any('outside the workspace' in r for r in reasons), reasons)

    def test_edit_set_must_equal_writable_set(self):
        (self.ws / 'b.py').write_text('B\n', encoding='utf-8')
        # 加一个计划未声明的 writable → 双向相等失败，调用方不能用宽契约绕过。
        norm2, rsn = zfs.normalize_file_scope(
            {'workspace': str(self.ws), 'readable_files': [], 'writable_files': ['a.py', 'b.py']},
            self.ws)
        self.assertEqual(rsn, [])
        reasons = zfs.plan_edit_coverage_reasons(['Edit(a.py)'], norm2, self.ws)
        self.assertTrue(any('does not exactly cover plan edits' in r for r in reasons), reasons)


class CrossLanguageShaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / 'ws'
        self.ws.mkdir()
        (self.ws / 'a.py').write_text('print(1)\n', encoding='utf-8')

    def test_python_freeze_sha_equals_js_recompute(self):
        if shutil.which('node') is None:
            self.skipTest('node not available')
        contract = {'workspace': str(self.ws), 'readable_files': [], 'writable_files': ['a.py']}
        norm, reasons = zfs.normalize_file_scope(contract, self.ws)
        self.assertEqual(reasons, [])
        py_sha = zfs.canonical_sha(norm)
        payload = self.ws / 'norm.json'
        payload.write_text(json.dumps(norm, ensure_ascii=False), encoding='utf-8')
        driver = f'''
import {{ pathToFileURL }} from 'node:url';
import {{ readFileSync }} from 'node:fs';
const mod = await import(pathToFileURL({json.dumps(str(ROOT / 'scripts' / 'zcode_file_scope.mjs'))}).href);
const obj = JSON.parse(readFileSync(process.argv[2], 'utf-8'));
process.stdout.write(JSON.stringify({{ sha: mod.canonicalSha(obj) }}) + '\\n');
'''
        res = _drive_node(driver, [str(payload)])
        self.assertEqual(res['sha'], py_sha)

    def test_js_canonicalize_matches_python_on_raw(self):
        if shutil.which('node') is None:
            self.skipTest('node not available')
        raw = {'b': 2, 'a': {'z': [1, 2, 3], 'y': 'x'}, 'c': 'ünïcode'}
        py = zfs.canonical_json(raw)
        driver = (
            "import { pathToFileURL } from 'node:url';\n"
            "import { readFileSync } from 'node:fs';\n"
            "const mod = await import(pathToFileURL(process.argv[2]).href);\n"
            "const obj = JSON.parse(readFileSync(process.argv[3], 'utf-8'));\n"
            "process.stdout.write(JSON.stringify({ canon: mod.canonicalize(obj) }) + '\\n');\n"
        )
        with tempfile.TemporaryDirectory() as td:
            dpath = Path(td) / 'driver.mjs'
            dpath.write_text(driver, encoding='utf-8')
            payload = Path(td) / 'raw.json'
            payload.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
            res = _drive_node(driver, [str(ROOT / 'scripts' / 'zcode_file_scope.mjs'), str(payload)])
            self.assertEqual(res['canon'], py)


class GuardedPortBehaviorTests(unittest.TestCase):
    DRIVER = r'''
import { pathToFileURL } from 'node:url';
import { readFileSync, realpathSync, statSync, existsSync } from 'node:fs';
import path from 'node:path';
const mod = await import(pathToFileURL(process.argv[2]).href);
const spec = JSON.parse(readFileSync(process.argv[3], 'utf-8'));
// 冻结绑定用的 SHA 必须覆盖 Python 原样冻结的 contract（runner 也是重算原始 request.contract），
// 而 buildFileScopeGuard 需要 node realpath 形式，故先留一份 pristine 供 canonicalSha 比对。
const pristineContract = JSON.parse(JSON.stringify(spec.contract));

// Windows 下 Python os.path.realpath 与 node realpathSync 对 TEMP 可能给出长名/8.3 短名两种
// 形式；真实登记工作区（用户主目录）二者一致，故本测试统一用 node realpath 再基，令守卫逻辑
// 在自洽路径集上验证（跨语言路径逐字节等价的关注点在 canonical SHA 用例单独覆盖）。
function nodeReal(p) {
  try { return realpathSync(p); } catch {
    const dir = path.dirname(p); const base = path.basename(p);
    try { return path.join(realpathSync(dir), base); } catch { return p; }
  }
}
spec.contract.workspace = nodeReal(spec.workspace);
for (const arr of [spec.contract.readable_files, spec.contract.writable_files])
  for (const e of arr) e.real = nodeReal(e.real);
for (const k of ['readableReal', 'writableExisting', 'writableNew', 'unregistered', 'readOnlyRef',
                 'outsidePath', 'existingParent', 'grandparentMissing'])
  spec[k] = nodeReal(spec[k]);

class FileSystemPortError extends Error {
  constructor(o) { super(o && o.message); Object.assign(this, o || {}); this.name = 'FileSystemPortError'; }
}
const innerCalls = [];
function makeInner() {
  const rec = (name) => (...a) => { innerCalls.push(name); return Promise.resolve(resultFor(name, a)); };
  function resultFor(name, a) {
    if (name === 'writeTextFile') return { bytesWritten: (a[0] && a[0].content ? String(a[0].content).length : 0) };
    if (name === 'readTextFile') return { content: 'READ-CONTENT', encoding: (a[1] && a[1].encoding) || 'utf8' };
    if (name === 'stat') return { isFile: () => true, size: 3 };
    return { ok: true };
  }
  return { stat: rec('stat'), readTextFile: rec('readTextFile'), readBinaryFile: rec('readBinaryFile'),
    readTextFileRange: rec('readTextFileRange'), writeTextFile: rec('writeTextFile'),
    createDirectory: rec('createDirectory'), removeFile: rec('removeFile'),
    listDirectory: rec('listDirectory'), searchFiles: rec('searchFiles'), searchText: rec('searchText') };
}

const workspaceReal = realpathSync(spec.workspace);
const guard = mod.buildFileScopeGuard(spec.contract,
  { workspaceRealPath: workspaceReal, FileSystemPortError });
let auditN = 0;
const sink = { record() { auditN += 1; } };
const port = mod.createGuardedFileSystemPort({ innerPort: makeInner(), guard, sink });

const results = {};
async function attempt(label, fn) {
  const before = innerCalls.length;
  let threw = false;
  try { await fn(); } catch (e) { threw = true; }
  results[label] = { threw, innerCalled: innerCalls.length > before };
}
const readTarget = spec.readableReal;
const writeTarget = spec.writableExisting;
const newTarget = spec.writableNew;

// 允许：登记读、stat、登记写（存在文件）、连续二次写（原子替换不误判越界）、新写（父目录已存在）
await attempt('read_ok', () => port.readTextFile({ path: readTarget }, { encoding: 'utf8' }));
await attempt('stat_ok', () => port.stat({ path: writeTarget }));
await attempt('write_existing_ok', () => port.writeTextFile({ path: writeTarget, content: 'x',
  expectedRevision: 'rev-7' }, { encoding: 'utf8' }));
await attempt('write_continuous_ok', () => port.writeTextFile({ path: writeTarget, content: 'y' },
  { encoding: 'utf8' }));
await attempt('write_new_ok', () => port.writeTextFile({ path: newTarget, content: 'z' },
  { encoding: 'utf8' }));

// 拒绝：未登记读、只读参考写、越界写、删除、列目录、搜索、未授权建目录
await attempt('read_unregistered_denied', () => port.readTextFile({ path: spec.unregistered },
  { encoding: 'utf8' }));
await attempt('write_readonly_ref_denied', () => port.writeTextFile({ path: spec.readOnlyRef,
  content: 'no' }, { encoding: 'utf8' }));
await attempt('write_outside_denied', () => port.writeTextFile({ path: spec.outsidePath, content: 'no' },
  { encoding: 'utf8' }));
await attempt('remove_denied', () => port.removeFile({ path: writeTarget }));
await attempt('list_denied', () => port.listDirectory({ path: workspaceReal }));
await attempt('searchfiles_denied', () => port.searchFiles({ path: workspaceReal, pattern: 'x' }));
await attempt('searchtext_denied', () => port.searchText({ path: workspaceReal, pattern: 'x' }));
await attempt('mkdir_ungranted_denied', () => port.createDirectory({ path: spec.grandparentMissing }));
// 允许：已存在的必要父目录建目录
await attempt('mkdir_parent_ok', () => port.createDirectory({ path: spec.existingParent }));

// 转发细节单独验证（createParents 强制 false + expectedRevision/encoding 透传）
const fwdPortCalls = [];
const fwdInner = { writeTextFile: async (r, o) => { fwdPortCalls.push({ r, o }); return { bytesWritten: 1 }; },
  stat: async () => ({}), readTextFile: async () => ({ content: '' }) };
const fwdPort = mod.createGuardedFileSystemPort({ innerPort: fwdInner, guard, sink: { record() {} } });
await fwdPort.writeTextFile({ path: newTarget, content: 'q', createParents: true, expectedRevision: 'rev-9' },
  { encoding: 'utf16le' });

const methods = port._fsMethods().slice().sort();
process.stdout.write(JSON.stringify({
  results, methods,
  counters: { ops: port._fsOps(), denied: port._fsDenied(), audit: port._fsAuditEvents() },
  forwarded: fwdPortCalls.length ? { createParents: fwdPortCalls[0].r.createParents,
    expectedRevision: fwdPortCalls[0].r.expectedRevision,
    encoding: fwdPortCalls[0].o && fwdPortCalls[0].o.encoding } : null,
  sha: mod.canonicalSha(pristineContract),
}) + '\n');
'''

    def setUp(self):
        # Windows 上 TEMP 常落在 `C:\Users\ADMINI~1\...` 短名，realpath 长/短名在 Python 与 node
        # 间不一致；改把受控端口行为测试的工作区放在无 8.3 别名的 Public 目录下令二者一致。
        base = (os.environ.get('PUBLIC') or 'C:\\Users\\Public') if sys.platform == 'win32' else None
        self._tmp = tempfile.mkdtemp(prefix='bw_fs_scope_', dir=base)
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.ws = Path(self._tmp) / 'ws'
        self.ws.mkdir()
        self.ext = Path(self._tmp) / 'ext'
        self.ext.mkdir()
        self.readable = self.ws / 'read.txt'
        self.readable.write_text('R\n', encoding='utf-8')
        self.writable_existing = self.ws / 'write.txt'
        self.writable_existing.write_text('W\n', encoding='utf-8')
        self.subdir = self.ws / 'sub'
        self.subdir.mkdir()
        self.new_target = self.subdir / 'new.txt'  # 不存在，父目录存在
        self.unregistered = self.ws / 'secret.txt'
        self.unregistered.write_text('S\n', encoding='utf-8')
        self.readonly_ref = self.ext / 'ref.txt'  # 工作区外只读参考
        self.readonly_ref.write_text('REF\n', encoding='utf-8')
        self.outside = self.ext / 'evil.txt'
        self.grandparent_missing = str(self.ws / 'no' / 'such' / 'dir')

        contract = {'workspace': str(self.ws),
                    'readable_files': ['read.txt', os.path.relpath(self.readonly_ref, self.ws)],
                    'writable_files': ['write.txt', 'sub/new.txt']}
        self.norm, reasons = zfs.normalize_file_scope(contract, self.ws)
        self.assertEqual(reasons, [])

    def _run(self):
        if shutil.which('node') is None:
            self.skipTest('node not available')
        read_real = next(e['real'] for e in self.norm['readable_files']
                         if e['real'].endswith('read.txt'))
        # 两个 writable：write.txt（已存在，带 sha）与 sub/new.txt（新文件，sha=None），按 real 排序后取。
        existing = next(e['real'] for e in self.norm['writable_files']
                        if e['real'].endswith('write.txt'))
        newreal = next(e['real'] for e in self.norm['writable_files']
                       if e['real'].endswith('new.txt'))
        spec = {'workspace': str(self.ws), 'contract': self.norm,
                'readableReal': read_real,
                'writableExisting': existing, 'writableNew': newreal,
                'unregistered': zfs._normcase(os.path.realpath(self.unregistered)),
                'readOnlyRef': zfs._normcase(os.path.realpath(self.readonly_ref)),
                'outsidePath': zfs._normcase(os.path.realpath(self.ext / 'evil.txt')),
                'existingParent': zfs._normcase(os.path.realpath(self.subdir)),
                'grandparentMissing': zfs._normcase(self.grandparent_missing)}
        payload = Path(self._tmp) / 'spec.json'
        payload.write_text(json.dumps(spec, ensure_ascii=False), encoding='utf-8')
        return _drive_node(GuardedPortBehaviorTests.DRIVER,
                           [str(ROOT / 'scripts' / 'zcode_file_scope.mjs'), str(payload)])

    def test_allows_registered_and_denies_the_rest(self):
        res = self._run()
        r = res['results']
        for label in ('read_ok', 'stat_ok', 'write_existing_ok', 'write_continuous_ok',
                      'write_new_ok', 'mkdir_parent_ok'):
            self.assertFalse(r[label]['threw'], (label, r[label]))
            self.assertTrue(r[label]['innerCalled'], (label, r[label]))
        for label in ('read_unregistered_denied', 'write_readonly_ref_denied', 'write_outside_denied',
                      'remove_denied', 'list_denied', 'searchfiles_denied', 'searchtext_denied',
                      'mkdir_ungranted_denied'):
            self.assertTrue(r[label]['threw'], (label, r[label]))
            self.assertFalse(r[label]['innerCalled'], (label, r[label]))

    def test_forwarding_forces_no_parent_creation_and_preserves_options(self):
        res = self._run()
        self.assertEqual(res['forwarded']['createParents'], False)
        self.assertEqual(res['forwarded']['expectedRevision'], 'rev-9')
        self.assertEqual(res['forwarded']['encoding'], 'utf16le')

    def test_method_surface_is_exactly_the_ten_port_methods(self):
        res = self._run()
        self.assertEqual(res['methods'], sorted(PORT_METHODS))

    def test_counters_reflect_ops_denials_and_audit(self):
        res = self._run()
        # 允许的内层操作计数（读/stat/连续写/新写/mkdir_parent）应 >0；拒绝项单独计数。
        self.assertGreater(res['counters']['ops'], 0)
        self.assertEqual(res['counters']['denied'], 8)
        self.assertGreater(res['counters']['audit'], 0)

    def test_sha_matches_python_freeze(self):
        if shutil.which('node') is None:
            self.skipTest('node not available')
        # buildFileScopeGuard 在 spec 里对同一 normalized contract 计算 sha，与 Python canonical_sha 一致。
        res = self._run()
        self.assertEqual(res['sha'], zfs.canonical_sha(self.norm))


class ParentAliasDriftTests(unittest.TestCase):
    """独立复现并锁定 S4 修复：登记新文件后、build 成功，再把父目录替换成指向同工作区另一目录的
    junction/symlink（同工作区内即可稳定出现的静态父目录别名漂移）。用真实目录 + 真实链接驱动真实
    scripts/zcode_file_scope.mjs，不 mock 被测守卫；证明 build 期拒绝已漂移父目录、运行期 write 拒绝
    且 inner 零调用、createDirectory 拒绝同一替换；并明确允许真实普通父目录里的新文件正常写入。"""

    DRIVER = r'''
import { pathToFileURL } from 'node:url';
import { execSync } from 'node:child_process';
import { readFileSync, realpathSync, statSync, rmdirSync, symlinkSync } from 'node:fs';
import path from 'node:path';
const mod = await import(pathToFileURL(process.argv[2]).href);
const spec = JSON.parse(readFileSync(process.argv[3], 'utf-8'));

function nodeReal(p) {
  try { return realpathSync(p); } catch {
    const dir = path.dirname(p); const base = path.basename(p);
    try { return path.join(realpathSync(dir), base); } catch { return p; }
  }
}
spec.contract.workspace = nodeReal(spec.workspace);
for (const arr of [spec.contract.readable_files, spec.contract.writable_files])
  for (const e of arr) e.real = nodeReal(e.real);
for (const k of ['aliasLink', 'aliasTarget', 'realNew', 'newTarget'])
  spec[k] = nodeReal(spec[k]);

class FileSystemPortError extends Error {
  constructor(o) { super(o && o.message); Object.assign(this, o || {}); this.name = 'FileSystemPortError'; }
}
const innerCalls = [];
function makeInner() {
  const rec = (name) => (...a) => { innerCalls.push(name);
    return Promise.resolve(name === 'writeTextFile' ? { bytesWritten: 1 } : { ok: true }); };
  return { stat: rec('stat'), readTextFile: rec('readTextFile'), readBinaryFile: rec('readBinaryFile'),
    readTextFileRange: rec('readTextFileRange'), writeTextFile: rec('writeTextFile'),
    createDirectory: rec('createDirectory'), removeFile: rec('removeFile'),
    listDirectory: rec('listDirectory'), searchFiles: rec('searchFiles'), searchText: rec('searchText') };
}
const workspaceReal = realpathSync(spec.workspace);
const guardOpts = { workspaceRealPath: workspaceReal, FileSystemPortError };

// 1) 真实普通父目录（尚未被替换）：build 守卫应成功。
let buildBeforeDrift = { threw: false };
try { var guard = mod.buildFileScopeGuard(spec.contract, guardOpts); }
catch (e) { buildBeforeDrift = { threw: true, message: String(e && e.message) }; }

const port = guard ? mod.createGuardedFileSystemPort({ innerPort: makeInner(), guard,
  sink: { record() {} } }) : null;

const results = { buildBeforeDrift };
async function attempt(label, fn) {
  const before = innerCalls.length;
  let threw = false;
  try { await fn(); } catch (e) { threw = true; }
  results[label] = { threw, innerCalled: innerCalls.length > before };
}

if (port) {
  // 2) 允许：真实普通父目录里的新文件正常写入（父目录未被别名替换）。
  await attempt('write_real_parent_ok', () => port.writeTextFile({ path: spec.realNew, content: 'q' },
    { encoding: 'utf8' }));

  // 3) 制造漂移：删除空的真实父目录 A，替换为指向同工作区 B 的 junction/symlink。
  let linkMade = false; let linkError = null;
  try {
    rmdirSync(spec.aliasLink);
    if (process.platform === 'win32') {
      execSync(`mklink /J "${spec.aliasLink}" "${spec.aliasTarget}"`, { stdio: 'ignore' });
    } else {
      symlinkSync(spec.aliasTarget, spec.aliasLink, 'dir');
    }
    linkMade = true;
  } catch (e) { linkError = String(e && e.message); }
  results.link = { made: linkMade, error: linkError };

  if (linkMade) {
    // 4) 运行期：写已漂移父目录下的登记新文件必须被拒，inner 零调用（别名会写到未登记的 B）。
    await attempt('runtime_write_after_swap_denied', () => port.writeTextFile(
      { path: spec.newTarget, content: 'X' }, { encoding: 'utf8' }));
    // 5) createDirectory 拒绝同一替换后的别名父目录。
    await attempt('runtime_mkdir_after_swap_denied', () => port.createDirectory({ path: spec.aliasLink }));
    // 6) build 期：守卫重新构建时拒绝已漂移父目录（fail-closed）。
    let buildAfterDrift = { threw: false };
    try { mod.buildFileScopeGuard(spec.contract, guardOpts); }
    catch (e) { buildAfterDrift = { threw: true, message: String(e && e.message) }; }
    results.buildAfterDrift = buildAfterDrift;
  }
}
process.stdout.write(JSON.stringify({ results }) + '\n');
'''

    def setUp(self):
        base = (os.environ.get('PUBLIC') or 'C:\\Users\\Public') if sys.platform == 'win32' else None
        self._tmp = tempfile.mkdtemp(prefix='bw_fs_parent_', dir=base)
        # 先注册 rmtree，再注册链接安全移除：addCleanup 逆序执行 → 先删链接（不 follow target），后删 temp。
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.ws = Path(self._tmp) / 'ws'
        self.ws.mkdir()
        self.a = self.ws / 'A'   # 登记新文件的真实父目录（build 后将被换成指向 B 的别名）
        self.a.mkdir()
        self.b = self.ws / 'B'   # 同工作区内另一真实目录，别名目标
        self.b.mkdir()
        self.c = self.ws / 'C'   # 始终为真实普通父目录（正例）
        self.c.mkdir()
        self.new_target = str(self.a / 'new.txt')       # A/new.txt：登记但此刻不存在
        self.real_new = str(self.c / 'new2.txt')        # C/new2.txt：登记但此刻不存在，父目录不漂移
        contract = {'workspace': str(self.ws), 'readable_files': [],
                    'writable_files': ['A/new.txt', 'C/new2.txt']}
        self.norm, reasons = zfs.normalize_file_scope(contract, self.ws)
        self.assertEqual(reasons, [])

        def _remove_link():
            # 安全移除 junction/symlink 本身，不 follow 到目标删除 B 的内容。
            if sys.platform == 'win32':
                subprocess.run(['cmd', '/c', 'rmdir', str(self.a)], capture_output=True)
            else:
                try:
                    if self.a.is_symlink():
                        os.unlink(self.a)
                except OSError:
                    pass
        self.addCleanup(_remove_link)

    def _run(self):
        if shutil.which('node') is None:
            self.skipTest('node not available')
        spec = {'workspace': str(self.ws), 'contract': self.norm,
                'aliasLink': zfs._normcase(os.path.realpath(self.a)),
                'aliasTarget': zfs._normcase(os.path.realpath(self.b)),
                'realNew': zfs._normcase(os.path.realpath(self.c) + os.sep + 'new2.txt'),
                'newTarget': zfs._normcase(os.path.realpath(self.a) + os.sep + 'new.txt')}
        payload = Path(self._tmp) / 'spec.json'
        payload.write_text(json.dumps(spec, ensure_ascii=False), encoding='utf-8')
        return _drive_node(ParentAliasDriftTests.DRIVER,
                           [str(ROOT / 'scripts' / 'zcode_file_scope.mjs'), str(payload)])

    def test_parent_alias_drift_denied_build_and_runtime(self):
        res = self._run()
        r = res['results']
        # 前置：未漂移时 build 成功（否则整场景无法构造，视为环境问题）。
        self.assertFalse(r['buildBeforeDrift']['threw'], r['buildBeforeDrift'])
        # 本机若无能力创建真实链接：如实报告，绝不用假 alias 替代真实行为。
        if not r['link']['made']:
            self.skipTest('cannot create real junction/symlink on this host: '
                          + str(r['link'].get('error')))
        # 真实普通父目录里的新文件必须正常写入（证明修复未收紧合法路径）。
        self.assertFalse(r['write_real_parent_ok']['threw'], r['write_real_parent_ok'])
        self.assertTrue(r['write_real_parent_ok']['innerCalled'], r['write_real_parent_ok'])
        # build 之后父目录被换成同工作区别名：build 期重新构建必须拒绝已漂移父目录。
        self.assertTrue(r['buildAfterDrift']['threw'], r['buildAfterDrift'])
        # 运行期写已漂移父目录下的登记新文件必须被拒且 inner 零调用（否则写入未登记的 B/new.txt）。
        self.assertTrue(r['runtime_write_after_swap_denied']['threw'], r['runtime_write_after_swap_denied'])
        self.assertFalse(r['runtime_write_after_swap_denied']['innerCalled'],
                         r['runtime_write_after_swap_denied'])
        # createDirectory 拒绝同一替换后的别名父目录，inner 零调用。
        self.assertTrue(r['runtime_mkdir_after_swap_denied']['threw'], r['runtime_mkdir_after_swap_denied'])
        self.assertFalse(r['runtime_mkdir_after_swap_denied']['innerCalled'],
                         r['runtime_mkdir_after_swap_denied'])


if __name__ == '__main__':
    unittest.main()
