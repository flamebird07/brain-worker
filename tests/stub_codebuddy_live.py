# OFFLINE-SYNTHETIC-CODEBUDDY-LIVE-STUB v1
"""可移植合成 stub：为 scripts/codebuddy_live.py 的 run() 注入 `popen_factory`，在**进程内**
模拟 CodeBuddy CLI 的子进程句柄，零 OS 子进程、零网络、零真实账号/模型。

这不是生产可执行载体（生产是 codebuddy_live，仅认精确模型），只是离线单测里替换
`subprocess.Popen` 的假句柄工厂；行为完全由测试传入的固定规格决定，不执行任意代码。
"""
import subprocess


class FakeChild:
    """假子进程：communicate 返回预置 stdout/stderr 字节并设 returncode；可注入启动前异常、
    启动后异常、墙钟超时，用于验证释放/unknown 保留路径。"""

    def __init__(self, argv, *, stdout=b'', stderr=b'', returncode=0, pid=987654,
                 raise_on_launch=False, raise_after_start=False, timeout=False,
                 record=None):
        self.argv = argv
        self._stdout = stdout
        self._stderr = stderr
        self._returncode = returncode
        self.pid = pid
        self.returncode = None
        self.stdin_seen = None
        self.cwd_seen = None
        self.env_seen = None
        self.communicate_timeout = None
        self.killed = False
        self.raise_on_launch = raise_on_launch
        self.raise_after_start = raise_after_start
        self.timeout = timeout
        self._timed_out_once = False
        self._record = record if record is not None else {}

    def communicate(self, input=None, timeout=None):  # noqa: A002 - 镜像 Popen 形参
        self.communicate_timeout = timeout
        if self._record is not None:
            self._record['communicate_timeout'] = timeout
        if self.timeout and not self._timed_out_once:
            self._timed_out_once = True
            raise subprocess.TimeoutExpired(self.argv, timeout)
        if self.raise_after_start:
            raise RuntimeError('synthetic: terminal state could not be confirmed')
        self.stdin_seen = input
        self.returncode = self._returncode
        self._record['stdin_sha256_len'] = (len(input) if input is not None else 0)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True


def make_popen_factory(*, stdout=b'', stderr=b'', returncode=0, pid=987654,
                       raise_on_launch=False, raise_after_start=False, timeout=False,
                       record=None):
    """返回一个替换 subprocess.Popen 的工厂：记录 argv/cwd/env，供传输口径断言。"""

    def _factory(argv, **kwargs):
        if raise_on_launch:
            # 模拟 Popen 启动前失败：不返回子句柄 → run() 走 start_failed 释放自身占位。
            raise OSError('synthetic: Popen failed to launch')
        child = FakeChild(argv, stdout=stdout, stderr=stderr, returncode=returncode,
                          pid=pid, raise_on_launch=raise_on_launch,
                          raise_after_start=raise_after_start, timeout=timeout,
                          record=record)
        child.cwd_seen = kwargs.get('cwd')
        child.env_seen = kwargs.get('env')
        if record is not None:
            record['argv'] = list(argv)
            record['cwd'] = kwargs.get('cwd')
            record['shell'] = kwargs.get('shell', False)
            record['env_disable_autoupdater'] = (kwargs.get('env') or {}).get(
                'DISABLE_AUTOUPDATER')
        child.last_kwargs = kwargs
        return child

    return _factory
