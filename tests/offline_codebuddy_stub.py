# OFFLINE-SYNTHETIC-CODEBUDDY-STUB v1
"""仓库可信的固定合成 CodeBuddy stub（仅离线回放，绝不接真实运行时/账号/网络）。

本文件是 tests/offline_codebuddy_harness.py 唯一信任的可执行载体。harness 在任何回放
（含 replay_dispatch）之前，按**仓库字节**核验 config.cli：其磁盘内容 sha256 必须等于本
文件当前字节的 sha256（信任锚是仓库内的本文件，环境变量无法定义或放宽该白名单）。

行为只由环境**数据**参数化，绝不执行调用方提供的任意代码：
- CODEBUDDY_STUB_SPEC：JSON 规格，描述要回放的 stdout 行 / 原始 base64 段 / stderr /
  退出码（模拟 `node <cli>` 的 stream-json 输出形状）；
- CODEBUDDY_STUB_RECORD：可选，写入本次 argv/cwd/stdin 摘要，供测试断言传输口径；
- CODEBUDDY_STUB_STDIN_DUMP：可选，把收到的 stdin 原字节逐字落盘，供载荷字节级回归；
- BARRIER_DIR / BARRIER_TOTAL / TASK_ID：可选并行 barrier，仅用于证明多子进程真实重叠，
  路径与计数只来自环境数据，逻辑固定在本文件内。
"""
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def _barrier():
    """可选并行 barrier：仅当测试显式提供 BARRIER_DIR/BARRIER_TOTAL/TASK_ID 时参与。"""
    bdir = os.environ.get('BARRIER_DIR')
    total = os.environ.get('BARRIER_TOTAL')
    tid = os.environ.get('TASK_ID')
    if not bdir or not total or not tid:
        return
    b = Path(bdir)
    want = int(total)
    (b / ('started-' + tid)).write_text(str(time.time()))
    deadline = time.time() + 40
    while time.time() < deadline:
        if len(list(b.glob('started-*'))) >= want:
            break
        time.sleep(0.03)
    (b / ('finished-' + tid)).write_text(str(time.time()))


def main():
    record_path = os.environ.get('CODEBUDDY_STUB_RECORD')
    dump_path = os.environ.get('CODEBUDDY_STUB_STDIN_DUMP')
    spec_path = os.environ.get('CODEBUDDY_STUB_SPEC')
    stdin_bytes = sys.stdin.buffer.read()
    if dump_path:
        with open(dump_path, 'wb') as f:
            f.write(stdin_bytes)
    if record_path:
        record = {
            'argv': sys.argv,
            'cwd': os.getcwd(),
            'stdin_sha256': hashlib.sha256(stdin_bytes).hexdigest(),
            'stdin_len': len(stdin_bytes),
            'stdin_has_crlf': b'\r\n' in stdin_bytes,
            'env_disable_autoupdater': os.environ.get('DISABLE_AUTOUPDATER'),
            'max_retries_env': os.environ.get('CODEBUDDY_MAX_RETRIES'),
            'watchdog_env': os.environ.get('CODEBUDDY_RETRY_WATCHDOG'),
        }
        with open(record_path, 'wb') as f:
            f.write(json.dumps(record, ensure_ascii=False).encode('utf-8'))
    _barrier()
    spec = {}
    if spec_path:
        with open(spec_path, 'rb') as f:
            spec = json.loads(f.read().decode('utf-8'))
    out = sys.stdout.buffer
    for b64 in spec.get('stdout_raw_b64', []):
        out.write(base64.b64decode(b64))
        out.write(b'\n')
    for line in spec.get('stdout', []):
        out.write(line.encode('utf-8'))
        out.write(b'\n')
    out.flush()
    sys.stderr.buffer.write(spec.get('stderr', '').encode('utf-8'))
    sys.stderr.buffer.flush()
    sys.exit(spec.get('exit', 0))


if __name__ == '__main__':
    main()
