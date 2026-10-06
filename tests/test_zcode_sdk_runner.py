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
            receipt=(work/'receipt.txt').read_text().splitlines()
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

if __name__=='__main__':
    unittest.main()
