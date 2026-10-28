"""测试专用：临时切换「共享派发档位」（strong/medium/weak）的最小工具。

BW-MANUAL-THREE-MODE-20261023：档位是**真实状态文件**（默认
`~/.brain-worker/dispatch-mode.json`，可用 `BRAIN_WORKER_DISPATCH_MODE_FILE` 覆盖）。
测试必须用显式临时文件，绝不读写真实用户状态；`tests/conftest.py` 已把默认路径指到
临时目录下的不存在文件（→ 默认 weak），本工具用于需要具体档位的用例，退出时恢复原
环境变量并删除临时目录。

用法：
    from dispatch_mode_support import dispatch_mode
    with dispatch_mode('strong'):
        ...
"""
import contextlib
import json
import os
import tempfile
from pathlib import Path

ENV_VAR = 'BRAIN_WORKER_DISPATCH_MODE_FILE'


def insert_sonus_leftover(store, *, token='sonus-leftover', task_id='sonus-leftover',
                          state='reserved', reserved_at_utc=None):
    """插入一条「strong 时代派出、切档后仍在途」的 qoder:Sonus attempt。

    BW-STRONG-SONUS-20261024：weak 档永远不能**新派** Sonus（档位硬排除），所以弱档
    最多只能真实填出 15 个新派槽；要让「全局物理总 16」的总守卫场景成立，必须按真实
    形态直插一条 strong 遗留的 Sonus 在途行（切档不追杀、不假释放）。测试专用，绝不
    在弱档经 reserve/consume 伪造 Sonus 派发。"""
    import dispatch_pool as dp
    conn = dp.connect(store)
    try:
        conn.execute(
            'INSERT INTO attempts(token, task_id, runtime, model, pool_key, state, '
            'reserved_at_utc) VALUES(?,?,?,?,?,?,?)',
            (token, task_id, 'qoder', 'Sonus', dp.SONUS_POOL_KEY, state,
             reserved_at_utc or dp._iso(dp.utcnow())))
        conn.commit()
    finally:
        conn.close()


def weak_new_dispatch_capacity():
    """weak 档可新派容量（原 15）：Sonus 不在 weak 候选，永不贡献弱档新派名额。"""
    import dispatch_mode as dm
    import dispatch_pool as dp
    return sum(dp.capacity_for(k) for k in dm.ALL_POOL_KEYS)


@contextlib.contextmanager
def dispatch_mode(mode, *, raw=None, corrupt=False, unreadable=False):
    """把档位文件临时指向一个隔离副本。

    - 普通用法 `dispatch_mode('strong')`：写入 {"mode": "strong"}；
    - `corrupt=True`：写入非法 JSON（验证安全回落 weak）；
    - `raw='...'`：写入任意原始文本（验证非法值/非对象）；
    - `unreadable=True`：路径指向一个目录（读取必然失败），验证读失败回落。
    """
    tmp = tempfile.TemporaryDirectory(prefix='bw-mode-')
    path = Path(tmp.name) / 'dispatch-mode.json'
    if unreadable:
        path.mkdir()
    elif corrupt:
        path.write_text('{not json', encoding='utf-8')
    elif raw is not None:
        path.write_text(raw, encoding='utf-8')
    else:
        path.write_text(json.dumps({'mode': mode}), encoding='utf-8')
    saved = os.environ.get(ENV_VAR)
    os.environ[ENV_VAR] = str(path)
    try:
        yield path
    finally:
        if saved is None:
            os.environ.pop(ENV_VAR, None)
        else:
            os.environ[ENV_VAR] = saved
        tmp.cleanup()


@contextlib.contextmanager
def user_disabled_pools(keys):
    """BW-QODERCN-TOKEN-MAX-DISABLE-20261028：临时改写 `USER_DISABLED_POOL_KEYS`。

    仅用于把**历史口径**（qodercn:Qwen-3.8-Max 尚可新派的时代）或**物理容量登记**场景
    （如填满全部 16 槽触发 Luna 阈值/总守卫）离线复现的旧测试：当前生产集合为
    `{qodercn:Qwen-3.8-Max}`，凡需目标池参与候选/占位的构造场景必须显式包裹本上下文并
    写明缘由，退出恢复真实集合。所有消费方（dispatch_pool / worker_status_widget /
    qoder_direct）都经 `dispatch_mode` 模块属性读取，改属性即全局生效；绝不用于把目标
    重新加回**现行**候选断言（现行断言必须期待其它候选或禁用拒绝）。"""
    import dispatch_mode as dm
    saved = dm.USER_DISABLED_POOL_KEYS
    dm.USER_DISABLED_POOL_KEYS = keys
    try:
        yield
    finally:
        dm.USER_DISABLED_POOL_KEYS = saved
