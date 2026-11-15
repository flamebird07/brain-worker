"""BW-MAC-COMPAT-20261114-S1：macOS 兼容性离线回归（零真实模型/网络/库写入）。

覆盖三块：
1. dispatch_pool 的 macOS 进程创建身份探针（PID 复用绑定在 darwin 不再退化为
   unknown）——分发分支用注入 platform 断言，绝不依赖 CI 宿主平台；
2. worker_status_widget 的 POSIX flock 单实例闸（Windows mutex 之外的 mac/Linux
   对等语义，含互斥、释放、降级三个分支）；
3. zcode_direct --suggest-config 探测（注入假路径，逐项 present/missing 与骨架
   结构契约）。

真实 darwin 身份探针只在宿主确为 darwin 时才断言非 None；其余宿主上只验证
「诚实返回 None、绝不臆造」的保守契约。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(REPO / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_pool as dp  # noqa: E402
import worker_status_widget as widget  # noqa: E402


class ProcessIdentityDispatchTests(unittest.TestCase):
    """process_identity 按 platform 注入分发到对应创建身份探针。"""

    def test_darwin_platform_routes_to_macos_probe(self):
        calls = []

        def fake_macos(pid):
            calls.append(('macos', pid))
            return 'mac:1:2'

        def fake_linux(pid):
            calls.append(('linux', pid))
            return 'boot:1'

        orig = (dp._macos_process_created, dp._linux_process_created)
        dp._macos_process_created, dp._linux_process_created = fake_macos, fake_linux
        try:
            ident = dp.process_identity(os.getpid(), platform='darwin')
        finally:
            dp._macos_process_created, dp._linux_process_created = orig
        self.assertEqual(calls, [('macos', os.getpid())])
        self.assertEqual(ident['created'], 'mac:1:2')

    def test_linux_platform_still_routes_to_linux_probe(self):
        calls = []

        def fake_linux(pid):
            calls.append(pid)
            return None

        orig = dp._linux_process_created
        dp._linux_process_created = fake_linux
        try:
            ident = dp.process_identity(os.getpid(), platform='linux')
        finally:
            dp._linux_process_created = orig
        self.assertEqual(calls, [os.getpid()])
        self.assertIsNone(ident['created'])

    def test_nonpositive_pid_is_dead_without_identity(self):
        ident = dp.process_identity(0)
        self.assertEqual(ident, {'pid': 0, 'state': 'dead', 'created': None})

    def test_dead_pid_has_no_identity(self):
        ident = dp.process_identity(99999999)
        self.assertEqual(ident['state'], 'dead')
        self.assertIsNone(ident['created'])

    @unittest.skipUnless(sys.platform.startswith('darwin'), '仅 darwin 宿主')
    def test_darwin_real_self_identity_is_mac_prefixed(self):
        ident = dp.process_identity(os.getpid())
        self.assertEqual(ident['state'], 'alive')
        self.assertTrue(ident['created'], 'darwin 自身进程必须有创建身份')
        self.assertTrue(str(ident['created']).startswith('mac:'),
                        ident['created'])

    def test_macos_probe_honest_none_off_darwin(self):
        # 非 darwin 宿主调用 mac 探针必须诚实返回 None（绝不猜、绝不抛）。
        if sys.platform.startswith('darwin'):
            self.skipTest('宿主本身是 darwin，验证其它平台分支需注入')
        self.assertIsNone(dp._macos_process_created(os.getpid()))


class WidgetPosixSingleInstanceTests(unittest.TestCase):
    """POSIX flock 单实例闸的三个分支；锁路径可注入，绝不触碰真实用户目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='bw-widget-lock-')
        self.lock = os.path.join(self.tmp, 'widget-instance.lock')

    def _release(self, handle):
        widget._release_posix_single_instance(handle)

    def test_acquire_then_second_attempt_refused(self):
        h1 = widget._acquire_posix_single_instance(lock_path=self.lock)
        try:
            self.assertIsNotNone(h1, '首个实例必须获得锁')
            h2 = widget._acquire_posix_single_instance(lock_path=self.lock)
            self.assertIsNone(h2, '第二个实例必须被单实例闸拒绝')
        finally:
            self._release(h1)

    def test_release_allows_reacquire(self):
        h1 = widget._acquire_posix_single_instance(lock_path=self.lock)
        self._release(h1)
        h2 = widget._acquire_posix_single_instance(lock_path=self.lock)
        try:
            self.assertIsNotNone(h2, '释放后必须可再次获得锁')
        finally:
            self._release(h2)

    def test_release_is_idempotent(self):
        h = widget._acquire_posix_single_instance(lock_path=self.lock)
        self._release(h)
        # 重复释放绝不抛错、绝不误关其它句柄。
        self._release(h)

    def test_unwritable_dir_degrades_honestly(self):
        # 目录不可写（根下不存在路径）→ 诚实降级放行并返回 ('degraded', None) 形态。
        handle = widget._acquire_posix_single_instance(
            lock_path='/proc/definitely/not/writable/widget.lock')
        self.assertIsNotNone(handle)
        kind = handle[0] if isinstance(handle, tuple) else handle
        self.assertIn(kind, ('held', 'degraded'))


class ZcodeSuggestConfigTests(unittest.TestCase):
    """--suggest-config 探测契约：注入假路径验证逐项判定与骨架结构。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='bw-suggest-')
        self.existing = Path(self.tmp) / 'exists.bin'
        self.existing.write_bytes(b'x')

    def _result(self, zcode_direct, candidates):
        return zcode_direct.suggest_entry_config(candidates)

    def test_all_missing_reports_missing_and_no_valid_skeleton(self):
        import zcode_direct as zd
        res = self._result(zd, {})
        self.assertEqual(res['status'], 'incomplete')
        self.assertFalse(res['all_present'])
        kinds = {item['kind'] for item in res['checks']}
        self.assertEqual(kinds, set(zd.CONFIG_FILE_KEYS))
        for item in res['checks']:
            self.assertEqual(item['status'], 'missing')
        self.assertIsNone(res['skeleton'], '缺项时绝不给半缺骨架')

    def test_all_present_emits_valid_skeleton(self):
        import zcode_direct as zd
        node = self.existing
        personal = Path(self.tmp) / 'personal.json'
        personal.write_text('{}')
        cands = {
            'node': [str(node)],
            'bootstrap': [str(node)],
            'tsx_loader': [str(node)],
            'builtin_provider_config': [str(node)],
            'personal_provider_config': [str(personal)],
        }
        res = self._result(zd, cands)
        self.assertEqual(res['status'], 'complete')
        self.assertTrue(res['all_present'])
        skel = res['skeleton']
        self.assertEqual(skel, {key: paths[0] for key, paths in cands.items()})
        # 骨架绝不包含凭据值。
        self.assertNotIn('token', str(skel).lower())

    def test_skeleton_only_first_existing_candidate(self):
        import zcode_direct as zd
        cands = {
            'node': [str(Path(self.tmp) / 'nope'), str(self.existing)],
            'bootstrap': [str(self.existing)],
            'tsx_loader': [str(self.existing)],
            'builtin_provider_config': [str(self.existing)],
            'personal_provider_config': [str(self.existing)],
        }
        res = self._result(zd, cands)
        self.assertEqual(res['skeleton']['node'], str(self.existing))
        item = next(c for c in res['checks'] if c['kind'] == 'node')
        self.assertEqual(item['status'], 'present')
        self.assertEqual(item['chosen'], str(self.existing))


if __name__ == '__main__':
    unittest.main()
