"""pytest 全局隔离：让测试**永不读写真实用户档位文件**。

BW-MANUAL-THREE-MODE-20261023：dispatch_mode 默认路径是 `~/.brain-worker/dispatch-mode.json`。
若不隔离，测试结果会取决于本机真实档位（用户可能已手动选过 strong），既不确定也可能
意外改写用户状态。这里在测试会话开始时把 `BRAIN_WORKER_DISPATCH_MODE_FILE` 指向一个
临时目录内的**不存在**文件，于是默认口径恒为 weak（缺文件 → 安全回落默认档）。

需要验证具体档位的用例自行 `os.environ[...] = <临时路径>`（并写入 JSON），用完恢复；
它们写的是自己的临时文件，绝不会碰到真实用户文件。
"""
import os
import tempfile

_TMP_DIR = tempfile.mkdtemp(prefix='bw-dispatch-mode-')
os.environ['BRAIN_WORKER_DISPATCH_MODE_FILE'] = os.path.join(_TMP_DIR,
                                                             'dispatch-mode.json')
