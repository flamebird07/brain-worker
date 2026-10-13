"""Run the actual JS SDK adapter against a local bootstrap double, without login/network."""
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
import prompt_contract as pc
import zcode_file_scope as zfs
MOCK = r'''
import {appendFileSync} from 'node:fs';
const mark = text => appendFileSync(process.env.RECEIPT, text + '\n');
const mode = process.env.CASE;
const selection = {providerId:'account:test', modelId:'test-model', options:{reasoningLevel:'low'}};
export async function startProcessProviderRegistryRuntime() {
 mark('registry');
 return {runtime:{registryService:{
 getView:()=>({providers:[{providerId:selection.providerId, models:[{modelId:selection.modelId}]}]}),
 validateSelection:()=>({ok:true})}}, dispose:()=>mark('dispose')};
}
export async function createZCodeApp() {
 mark('app');
 return {sessionId:'sess-test',
 listModels:()=>[{ref:selection}], setModel:async()=>{},
 getCurrentModelOption:()=>({ref:mode==='mismatch'?{...selection,modelId:'wrong'}:selection}),
 getModel:()=> 'account:test/test-model',
 runtime:{getToolRegistry:()=>({list:()=>['Read','Write','SecretDynamic']})},
 submitPrompt:async(prompt,opts)=>{mark('submit|'+JSON.stringify(prompt));
 if(!opts.toolDisallowlist.includes('SecretDynamic')) throw Error('dynamic tool leaked');
 opts.onEvent({sessionId:'sess-test',turnId:'turn-test',type:'model_request',payload:{providerId:'account:test',modelId:mode==='request-mismatch'?'wrong':'test-model'}});
 if(mode!=='no-terminal') opts.onEvent({sessionId:'sess-test',turnId:'turn-test',type:'turn_complete',payload:{resultType:'success',response:'original\r\nreport'}});
 return {response:'original\r\nreport',turnId:'turn-test',projection:{status:mode==='idle'?'idle':'completed'},usage:{inputTokens:3,outputTokens:2,totalTokens:5}};},
 close:async()=>mark('close')};
}
'''

class ActualRunnerTests(unittest.TestCase):
    def run_case(self, case, preflight=False, prompt='test'):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            bootstrap = work / 'bootstrap.mjs'
            bootstrap.write_text(MOCK, encoding='utf-8')
            config = work / 'config.json'
            config.write_text('{"environment":{}}', encoding='utf-8')
            request = {'entry_config':str(config), 'output_dir':str(work), 'bootstrap':str(bootstrap),
                       'builtin_provider_config':'unused', 'personal_provider_config':'unused',
                       'selection':{'providerId':'account:test','modelId':'test-model','options':{'reasoningLevel':'low'}},
                       'stage':'test','workspace':str(work), 'mode':'plan','allowed_tools':['Read'],
                       'tool_disallowlist_base':['Write'],'preflight_only':preflight,'prompt':prompt}
            path = work / 'request.json'
            path.write_text(json.dumps(request), encoding='utf-8')
            env = {**os.environ, 'CASE':case, 'RECEIPT':str(work/'receipt.txt')}
            process = subprocess.run([shutil.which('node'),str(ROOT/'scripts/zcode_sdk_runner.mjs'),
                                      '--request',str(path)],env=env,capture_output=True,timeout=30)
            receipt=(work/'receipt.txt').read_text(encoding='utf-8').splitlines()
            stdout=process.stdout.decode('utf-8').strip().splitlines()
            envelope=json.loads(stdout[0])
            self.assertEqual(len(stdout),1)
            response=(work/'response.md').read_bytes() if (work/'response.md').exists() else None
            return process.returncode,envelope,receipt,response

    def test_preflight_zero_app_submit_and_cleanup(self):
        code, _, receipt, _=self.run_case('ok',True)
        self.assertEqual(code,0)
        self.assertEqual(receipt,['registry','dispose'])

    def test_complete_composed_payload_reaches_actual_sdk_adapter(self):
        prompt = pc.compose_task_payload(pc.build_contract('SDK-CONTRACT-01', '/isolated/example'), '真实提交边界\n尾部保留\n')
        code, envelope, receipt, _ = self.run_case('ok', prompt=prompt)
        self.assertEqual(code, 0)
        self.assertTrue(envelope['submitted'])
        submits = [line.removeprefix('submit|') for line in receipt if line.startswith('submit|')]
        self.assertEqual(len(submits), 1)
        self.assertEqual(json.loads(submits[0]), prompt)
        self.assertTrue(prompt.endswith('尾部保留\n'))

    def test_success_original_response_and_cleanup(self):
        code, envelope, receipt, response=self.run_case('ok')
        self.assertEqual(code,0)
        self.assertTrue(envelope['ok'])
        self.assertEqual(response,b'original\r\nreport')
        # receipts 记录 submitPrompt 实际收到的 prompt 值，证明真实 request.prompt 被消费。
        self.assertEqual(receipt,['registry','app','submit|"test"','close','dispose'])

    def test_mismatch_zero_submit_and_cleanup(self):
        code, envelope, receipt, _=self.run_case('mismatch')
        self.assertNotEqual(code,0)
        self.assertFalse(envelope['submitted'])
        self.assertEqual(receipt,['registry','app','close','dispose'])

    def test_idle_with_matching_terminal_is_success(self):
        code, envelope, _, _ = self.run_case('idle')
        self.assertEqual(code, 0)
        self.assertTrue(envelope['terminal_success'])

    def test_without_terminal_is_failure(self):
        code, envelope, _, _ = self.run_case('no-terminal')
        self.assertNotEqual(code, 0)
        self.assertFalse(envelope['ok'])

    def test_actual_model_request_mismatch_is_failure(self):
        code, envelope, _, _ = self.run_case('request-mismatch')
        self.assertNotEqual(code, 0)
        self.assertFalse(envelope['ok'])

SCOPE_MOCK = r'''
import {appendFileSync} from 'node:fs';
const mark = t => appendFileSync(process.env.RECEIPT, t + '\n');
const selection = {providerId:'account:test', modelId:'test-model', options:{reasoningLevel:'low'}};
export async function startProcessProviderRegistryRuntime() {
  mark('registry');
  return {runtime:{registryService:{
    getView:()=>({providers:[{providerId:selection.providerId, models:[{modelId:selection.modelId}]}]}),
    validateSelection:()=>({ok:true})}}, dispose:()=>mark('dispose')};
}
export async function createZCodeApp(opts) {
  mark('app');
  const port = opts && opts.fileSystemPort;
  const surface = port ? Object.keys(port).filter(k => !k.startsWith('_')).sort() : [];
  mark('hasport:' + (port ? 'yes' : 'no') + '|n=' + surface.length);
  return {sessionId:'sess-test',
    listModels:()=>[{ref:selection}], setModel:async()=>{},
    getCurrentModelOption:()=>({ref:selection}), getModel:()=>'account:test/test-model',
    // 活 catalog 故意含 Bash/Glob/Grep/动态工具：runner 必须据 allowed 关掉它们。
    runtime:{getToolRegistry:()=>({list:()=>['Read','Edit','Write','Bash','Glob','Grep','SecretDynamic']})},
    submitPrompt:async(prompt,o)=>{mark('submit');
      try{ await port.readTextFile({path:process.env.FS_REG},{}); mark('readok'); }catch{ mark('readno'); }
      try{ await port.readTextFile({path:process.env.FS_UNREG},{}); mark('unregok'); }catch{ mark('unregdenied'); }
      if(o.toolDisallowlist.includes('Bash') && o.toolDisallowlist.includes('SecretDynamic')) mark('dynClosed');
      o.onEvent({sessionId:'sess-test',turnId:'turn-test',type:'model_request',payload:{providerId:'account:test',modelId:'test-model'}});
      o.onEvent({sessionId:'sess-test',turnId:'turn-test',type:'turn_complete',payload:{resultType:'success',response:'r'}});
      return {response:'r',turnId:'turn-test',projection:{status:'completed'},usage:{inputTokens:1,outputTokens:1,totalTokens:2}};},
    close:async()=>mark('close')};
}
'''

ADAPTERS_MOCK = r'''
export function createNodeFileSystemAdapter() {
  const ok = async (x) => x;
  return { stat: async()=>({isFile:()=>true}), readTextFile: async()=>({content:'X'}),
    readBinaryFile: async()=>({}), readTextFileRange: async()=>({}),
    writeTextFile: async()=>({bytesWritten:1}), createDirectory: async()=>({}),
    removeFile: async()=>({}), listDirectory: async()=>({}), searchFiles: async()=>({}),
    searchText: async()=>({}) };
}
'''

CONTRACTS_MOCK = r'''
export class FileSystemPortError extends Error {
  constructor(o){ super(o && o.message); Object.assign(this, o || {}); this.name='FileSystemPortError'; }
}
'''

class FileScopeRunnerTests(unittest.TestCase):
    def setUp(self):
        # 受控端口走 node realpath：放在无 8.3 短名的 Public 目录下，令 Python 冻结路径与 node 一致。
        base = os.environ.get('PUBLIC') or 'C:\\Users\\Public'
        self.root = Path(tempfile.mkdtemp(prefix='bw_fs_runner_', dir=base))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.ws = self.root / 'ws'
        self.ws.mkdir()
        (self.ws / 'a.txt').write_text('A\n', encoding='utf-8')
        (self.ws / 'b.txt').write_text('B\n', encoding='utf-8')  # 未登记

    def _run(self, mode='edit'):
        contract = {'workspace': str(self.ws), 'readable_files': ['a.txt'], 'writable_files': []}
        norm, reasons = zfs.normalize_file_scope(contract, self.ws)
        self.assertEqual(reasons, [])
        adapters = self.root / 'adapters.mjs'; adapters.write_text(ADAPTERS_MOCK, encoding='utf-8')
        contracts = self.root / 'contracts.mjs'; contracts.write_text(CONTRACTS_MOCK, encoding='utf-8')
        bootstrap = self.root / 'bootstrap.mjs'; bootstrap.write_text(SCOPE_MOCK, encoding='utf-8')
        config = self.root / 'config.json'; config.write_text('{"environment":{}}', encoding='utf-8')
        request = {'entry_config': str(config), 'output_dir': str(self.root),
                   'bootstrap': str(bootstrap), 'builtin_provider_config': 'unused',
                   'personal_provider_config': 'unused',
                   'selection': {'providerId': 'account:test', 'modelId': 'test-model',
                                 'options': {'reasoningLevel': 'low'}},
                   'stage': 'test', 'workspace': str(self.ws), 'mode': mode,
                   'allowed_tools': ['Read', 'Edit', 'Write'], 'tool_disallowlist_base': [],
                   'prompt': 'x', 'file_scope_contract': norm,
                   'file_scope_contract_sha256': zfs.canonical_sha(norm),
                   'adapters_entry': str(adapters), 'contracts_entry': str(contracts)}
        rpath = self.root / 'request.json'; rpath.write_text(json.dumps(request), encoding='utf-8')
        env = {**os.environ, 'RECEIPT': str(self.root / 'receipt.txt'),
               'FS_REG': norm['readable_files'][0]['real'],
               'FS_UNREG': zfs._normcase(os.path.realpath(self.ws / 'b.txt'))}
        proc = subprocess.run([shutil.which('node'), str(ROOT / 'scripts' / 'zcode_sdk_runner.mjs'),
                               '--request', str(rpath)], env=env, capture_output=True, timeout=60)
        receipt = (self.root / 'receipt.txt').read_text(encoding='utf-8').splitlines() \
            if (self.root / 'receipt.txt').exists() else []
        stdout = proc.stdout.decode('utf-8').strip().splitlines()
        envelope = json.loads(stdout[0]) if stdout else {}
        return proc.returncode, envelope, receipt

    def test_guarded_port_injected_into_actual_app_and_gates_io(self):
        code, envelope, receipt = self._run()
        self.assertEqual(envelope['file_scope_present'], True)
        self.assertEqual(envelope['file_scope_sha256_match'], True)
        self.assertEqual(envelope['file_scope_ready'], True)
        self.assertEqual(sorted(envelope['file_scope_methods']),
                         sorted(['createDirectory', 'stat', 'readTextFile', 'readBinaryFile',
                                 'readTextFileRange', 'writeTextFile', 'removeFile', 'listDirectory',
                                 'searchFiles', 'searchText']))
        self.assertTrue(envelope['submitted'])
        self.assertIn('hasport:yes|n=10', receipt)
        self.assertIn('readok', receipt)      # 登记读放行
        self.assertIn('unregdenied', receipt)  # 未登记读被守卫拒、inner 未触达
        self.assertIn('dynClosed', receipt)   # Bash/Glob/Grep/动态工具被关
        # 未登记读被拒 → 守卫真实计数体现。
        self.assertGreater(envelope['file_scope_denied'], 0)

    def test_mode_plan_with_file_scope_fails_closed_zero_submit(self):
        # 受控文件模式要求 mode=edit；plan 令 setupFileScope fail-closed：不建 App、零提交。
        code, envelope, receipt = self._run(mode='plan')
        self.assertNotEqual(code, 0)
        self.assertEqual(envelope['file_scope_ready'], False)
        self.assertFalse(envelope['submitted'])
        self.assertNotIn('app', receipt)      # 在 createZCodeApp 之前就拒绝
        self.assertNotIn('submit', receipt)   # 零模型提交

if __name__=='__main__':
    unittest.main()
