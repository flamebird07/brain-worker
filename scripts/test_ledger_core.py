#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""台账共享核心与双后端一致性测试（BW-DUAL-05）。

覆盖统一语义契约：
  - 核心纯逻辑：评分转换/clamp、等级、名称归一（别名/未知/不合并版本）、
    幂等指纹（issues 顺序不敏感、evidence 敏感、生成字段不参与）。
  - 双后端 parity：相同语义事件序列经 local（追加序重放）与 feishu（事件链
    重放）各自适配后评分/步长/样本/等级一致；幂等判定一致。
  - 兼容：旧 local 直拼键事件按核心规则重算匹配（评分不变）；飞书历史
    中文字段经 _feishu_norm 后指纹一致；含 baseline 的链不误报。
  - 新统一行为：身份未知不可计分（含证据不足路径）。
全部离线：飞书侧仅用虚构配置与字段构造，不发任何网络请求。
从仓库根运行：python -m scripts.test_ledger_core
"""

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts import capability_ledger as feishu  # noqa: E402
from scripts import ledger_core as core  # noqa: E402
from scripts import local_ledger as local  # noqa: E402


def local_write_events(path, events):
    """按 local JSONL 格式写测试事件（测试数据准备，非绕锁写台账）。"""
    with io.open(path, "w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def local_event(eid, agent, model, outcome, evaluated_at, evidence="ev",
                etype="assessment", issues=None, recorded_at="2026-09-30T15:00:00+08:00"):
    return {
        "event_id": eid, "type": etype, "agent": agent, "model": model,
        "model_key": core.lenient_key(agent, model),
        "task": "审查", "stage": "S", "evaluated_at": evaluated_at,
        "outcome": outcome, "issues": issues or [], "evidence": evidence,
        "identity_confirmed": etype == "assessment",
        "resolved_from": "p-" + eid if eid.startswith("resolve:") else None,
        "recorded_at": recorded_at,
    }


def feishu_fields(eid, agent, model, outcome, evaluated_at, evidence="ev",
                  etype="assessment", issue="", recorded_at=None, chain=None):
    """构造飞书事件字段；chain 提供时附带事件链一致性字段（评分后等）。"""
    fields = {
        "事件ID": eid, "事件类型": etype,
        "模型键": core.lenient_key(agent, model) if etype != "identity_pending"
                 else core.lenient_key(agent, "待确认模型"),
        "桌面应用": agent, "模型/版本": model,
        "任务类型": "审查", "阶段编号": "S",
        "评估时间": evaluated_at, "记录时间": recorded_at or "2026-09-30T15:00:00.000000+08:00",
        "主脑结论": outcome, "证据": evidence,
    }
    if issue:
        fields["问题与限制"] = issue
    if chain:
        fields.update(chain)
    return fields


class TestCoreSemantics(unittest.TestCase):
    def test_transition_values_and_clamps(self):
        self.assertEqual(core.transition(50, 1, 0, "无问题"), (60, 2, 1, 10))
        self.assertEqual(core.transition(60, 2, 1, "轻微问题"), (55, 1, 2, -5))
        self.assertEqual(core.transition(55, 1, 2, "重大问题"), (40, 1, 3, -15))
        self.assertEqual(core.transition(20, 3, 3, "严重问题"), (0, 1, 4, -30))  # 步长归 1
        self.assertEqual(core.transition(0, 1, 4, "无问题"), (10, 2, 5, 10))
        self.assertEqual(core.transition(95, 4, 5, "无问题"), (100, 5, 6, 10))  # 双 clamp
        self.assertEqual(core.transition(50, 1, 2, "证据不足"), (50, 1, 2, 0))  # 不改分不加样本
        with self.assertRaises(core.LedgerCoreError):
            core.transition(50, 1, 0, "超纲结论")

    def test_level_tiers_and_recommend_gating(self):
        self.assertEqual(core.level(80, 3, ["无问题"] * 3), "推荐")
        self.assertEqual(core.level(80, 2, ["无问题"] * 2), "可用但需逐阶段审查（试用中）")  # 样本不足
        self.assertEqual(core.level(80, 4, ["无问题", "重大问题", "无问题"]), "可用但需逐阶段审查")
        self.assertEqual(core.level(85, 6, ["重大问题", "无问题", "无问题"]), "可用但需逐阶段审查")
        self.assertEqual(core.level(85, 6, ["无问题"] * 3), "推荐")  # 重大滑出最近 3 次
        self.assertEqual(core.level(65, 3, []), "可用但需逐阶段审查")
        self.assertEqual(core.level(45, 3, []), "观察")
        self.assertEqual(core.level(30, 3, []), "受限或暂不推荐")
        self.assertEqual(core.level(None, 0, []), "未定级")

    def test_canonical_known_aliases_only(self):
        # 已知别名（精确等价表）归一
        self.assertEqual(core.canonical_triple("ZCode Desktop", "GLM-5.3")[0], "ZCode")
        self.assertEqual(core.canonical_triple("Xiaomi MiMo 浏览器", "V2")[0], "Xiaomi MiMo")
        self.assertEqual(core.canonical_model("官方 GLM-5.3-Flash"), "ZCode 官方 GLM-5.3-Flash")
        self.assertEqual(core.canonical_model("kimi-k2.8-preview"), "火山 kimi-k2.8-preview")
        # 不模糊匹配、不合并不同版本（精确表外一律独立）
        self.assertEqual(core.canonical_model("官方 GLM-5.3-Flash 内测"), "官方 GLM-5.3-Flash 内测")
        self.assertNotEqual(core.lenient_key("ZCode", "GLM-5.3"), core.lenient_key("ZCode", "GLM-5.3-Flash"))
        self.assertNotEqual(core.lenient_key("ZCode", "V2.6"), core.lenient_key("ZCode", "V2.7"))
        # 未知身份：计分拒绝；pending 允许占位
        with self.assertRaises(core.LedgerCoreError):
            core.canonical_triple("ZCode", "未知")
        self.assertEqual(core.canonical_triple("ZCode", "未知", allow_pending=True)[1], "待确认模型")
        self.assertEqual(core.canonical_triple("ZCode", "", allow_pending=True)[1], "待确认模型")

    def test_fingerprint_semantics(self):
        base = {"event_id": "e1", "type": "assessment", "model_key": "ZCode / GLM",
                "task": "t", "stage": "S", "evaluated_at": "2026-09-30T10:00:00+08:00",
                "outcome": "无问题", "evidence": "ev", "issues": ["b", "a"]}
        same_shuffled = dict(base, issues=["a", "b"])
        self.assertEqual(core.idempotency_fingerprint(base), core.idempotency_fingerprint(same_shuffled))
        changed_evidence = dict(base, evidence="ev-CHANGED")
        self.assertNotEqual(core.idempotency_fingerprint(base), core.idempotency_fingerprint(changed_evidence))
        changed_issue = dict(base, issues=["a", "c"])
        self.assertNotEqual(core.idempotency_fingerprint(base), core.idempotency_fingerprint(changed_issue))
        # 字符串 issues 与等价单元素列表一致
        self.assertEqual(core.idempotency_fingerprint(dict(base, issues="x")),
                         core.idempotency_fingerprint(dict(base, issues=["x"])))
        # 生成字段不参与
        self.assertEqual(core.idempotency_fingerprint(base),
                         core.idempotency_fingerprint(dict(base, recorded_at="2099-01-01", _seq=99)))


class TestBackendParity(unittest.TestCase):
    """相同语义事件序列，两后端重放结果一致。"""

    SEQUENCE = [
        ("e1", "ZCode Desktop", "GLM-5.3", "无问题", "10:00:00"),   # 别名变体
        ("e2", "ZCode", "GLM-5.3", "无问题", "11:00:00"),
        ("e3", "ZCode", "GLM-5.3", "重大问题", "12:00:00"),
        ("e4", "ZCode", "GLM-5.3", "无问题", "13:00:00"),
        ("e5", "ZCode", "GLM-5.3", "证据不足", "14:00:00"),          # 不改分不加样本
        ("e6", "ZCode", "GLM-5.3", "无问题", "15:00:00"),
    ]
    # 预期：50→60→70→55(步长3→1)→65→65→75；样本 5；等级 可用但需逐阶段审查
    EXPECTED = (75, 3, 5, "可用但需逐阶段审查")

    def _local_state(self):
        with tempfile.TemporaryDirectory(prefix="bw-parity-l-") as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            local_write_events(path, [
                local_event(eid, agent, model, outcome, "2026-09-30T%s+08:00" % ts)
                for eid, agent, model, outcome, ts in self.SEQUENCE
            ])
            events = local.read_events(path)
            return local.status_payload(events, "ZCode", "GLM-5.3")

    def _feishu_state(self):
        # 预计算事件链一致性字段（state 会校验评分后/步长后/样本数后/评分变化）。
        chain_events = []
        score, step, samples = core.INIT_SCORE, core.INIT_STEP, core.INIT_SAMPLES
        for eid, agent, model, outcome, ts in self.SEQUENCE:
            score, step, samples, delta = core.transition(score, step, samples, outcome)
            chain_events.append(feishu_fields(
                eid, agent, model, outcome, "2026-09-30T%s+08:00" % ts,
                recorded_at="2026-09-30T15:00:0%d.000000+08:00" % (len(chain_events) + 1),
                chain={"评分后": score, "步长后": step, "样本数后": samples, "评分变化": delta}))
        all_events = [{"fields": f} for f in chain_events]
        st = feishu.state(all_events, "ZCode / GLM-5.3")
        return st, feishu.level(st["共享评分"], st["累计样本数"], st["最近三次结论"])

    def test_same_sequence_same_scores(self):
        lp = self._local_state()
        fs, fl = self._feishu_state()
        self.assertEqual((lp["共享评分"], lp["当前步长"], lp["累计样本数"], lp["能力等级"]),
                         self.EXPECTED)
        self.assertEqual((fs["共享评分"], fs["当前步长"], fs["累计样本数"], fl),
                         self.EXPECTED)
        self.assertEqual(lp["最近三次结论"], fs["最近三次结论"])

    def test_idempotency_decisions_match(self):
        # 跨后端指纹一致要求同形状语义输入：单 issue（local 单元素列表 ↔
        # feishu 单字符串字段）。多 issue 时 local 为列表、feishu 为单字符串
        # 字段——接口形状差异属契约允许的存储差异；幂等排序语义（顺序不
        # 敏感）已由核心单测覆盖，跨后端不做指纹互比。
        for outcome in ("无问题", "证据不足"):
            for l_issues, f_issue in ((["single-issue"], "single-issue"), ([], "")):
                lnorm = {"event_id": "x", "type": "assessment",
                         "model_key": core.lenient_key("ZCode", "GLM"),
                         "task": "审查", "stage": "S",
                         "evaluated_at": "2026-09-30T10:00:00+08:00",
                         "outcome": outcome, "evidence": "ev", "issues": l_issues}
                fnorm = feishu._feishu_norm(feishu_fields(
                    "x", "ZCode", "GLM", outcome, "2026-09-30T10:00:00+08:00",
                    issue=f_issue))
                self.assertEqual(core.idempotency_fingerprint(lnorm),
                                 core.idempotency_fingerprint(fnorm),
                                 "outcome=%s issues=%r" % (outcome, l_issues))

    def test_pending_then_resolve_parity(self):
        # local：pending 不计分 → resolve 计一次（pending 写实名但未确认，计数可见）
        with tempfile.TemporaryDirectory(prefix="bw-parity-pr-") as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            local_write_events(path, [
                local_event("p1", "Xiaomi MiMo 浏览器", "MiMo V2.6 Pro", "无问题",
                            "2026-09-30T10:00:00+08:00", etype="identity_pending"),
                local_event("resolve:p1", "Xiaomi MiMo", "MiMo V2.6 Pro", "无问题",
                            "2026-09-30T10:00:00+08:00", etype="assessment"),
            ])
            payload = local.status_payload(local.read_events(path), "Xiaomi MiMo", "MiMo V2.6 Pro")
            self.assertEqual((payload["共享评分"], payload["当前步长"], payload["累计样本数"]), (60, 2, 1))
            # pending 事件的键按重算匹配（agent 变体归一到同一应用）
            self.assertEqual(payload["待确认身份事件数"], 1)
        # feishu：同语义
        all_events = [
            {"fields": feishu_fields("p1", "Xiaomi MiMo", "待确认模型", "无问题",
                                     "2026-09-30T10:00:00+08:00", etype="identity_pending",
                                     recorded_at="2026-09-30T15:00:01.000000+08:00")},
            {"fields": feishu_fields("resolve:p1", "Xiaomi MiMo", "MiMo V2.6 Pro", "无问题",
                                     "2026-09-30T10:00:00+08:00",
                                     recorded_at="2026-09-30T15:00:02.000000+08:00",
                                     chain={"评分后": 60, "步长后": 2, "样本数后": 1, "评分变化": 10})},
        ]
        st = feishu.state(all_events, "Xiaomi MiMo / MiMo V2.6 Pro")
        self.assertEqual((st["共享评分"], st["当前步长"], st["累计样本数"]), (60, 2, 1))

    def test_agent_alias_resolve_accepted_mismatch_rejected(self):
        with tempfile.TemporaryDirectory(prefix="bw-parity-am-") as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            local_write_events(path, [
                local_event("p1", "ZCode Desktop", "未知", "无问题",
                            "2026-09-30T10:00:00+08:00", etype="identity_pending"),
            ])
            import argparse
            ns = argparse.Namespace(
                pending_event_id="p1", agent="ZCode", model="GLM",
                identity_evidence="user-confirm", identity_confirmed=True)
            # 归一后同一应用（ZCode Desktop → ZCode）：接受；写入后计一次。
            with contextlib.redirect_stdout(io.StringIO()):
                local.cmd_resolve(ns, path)
            payload = local.status_payload(local.read_events(path), "ZCode", "GLM")
            self.assertEqual(payload["累计样本数"], 1)
            # 归一后不同应用：拒绝
            local_write_events(path, [
                local_event("p2", "ZCode", "未知", "无问题",
                            "2026-09-30T11:00:00+08:00", etype="identity_pending"),
            ])
            ns2 = argparse.Namespace(
                pending_event_id="p2", agent="OtherDesk", model="GLM",
                identity_evidence="x", identity_confirmed=True)
            with self.assertRaisesRegex(local.LedgerError, "不匹配"):
                with contextlib.redirect_stdout(io.StringIO()):
                    local.cmd_resolve(ns2, path)


class TestReplayOrderAndCompat(unittest.TestCase):
    def test_same_second_and_cross_tz_append_order(self):
        with tempfile.TemporaryDirectory(prefix="bw-order-") as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            # 同秒 recorded_at + evaluated_at 乱序 + 跨时区 recorded_at：按追加序。
            local_write_events(path, [
                local_event("late+", "ZCode", "GLM", "无问题", "2026-09-30T20:00:00+08:00",
                            recorded_at="2026-09-30T15:00:00+08:00"),
                local_event("early-", "ZCode", "GLM", "严重问题", "2026-09-30T09:00:00+08:00",
                            recorded_at="2026-09-30T15:00:00+08:00"),
                local_event("utc3", "ZCode", "GLM", "无问题", "2026-09-30T08:00:00+00:00",
                            recorded_at="2026-09-30T14:00:00+00:00"),
            ])
            payload = local.status_payload(local.read_events(path), "ZCode", "GLM")
            # 追加序：50+10=60 → 60-30=30(步长1) → 40(步长2)
            self.assertEqual((payload["共享评分"], payload["当前步长"]), (40, 2))

    def test_legacy_local_direct_key_events_still_match(self):
        """旧版直拼键事件（无归一）按核心重算匹配，评分不变。"""
        with tempfile.TemporaryDirectory(prefix="bw-compat-") as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            legacy = [
                {"event_id": "old1", "type": "assessment", "agent": "ZCode Desktop",
                 "model": "GLM", "model_key": "ZCode Desktop / GLM",  # 旧直拼键
                 "task": "审查", "stage": "S", "evaluated_at": "2026-09-30T10:00:00+08:00",
                 "outcome": "无问题", "issues": [], "evidence": "e", "identity_confirmed": True,
                 "resolved_from": None, "recorded_at": "2026-09-28T10:00:00+08:00"},
            ]
            local_write_events(path, legacy)
            payload = local.status_payload(local.read_events(path), "ZCode", "GLM")
            self.assertEqual((payload["共享评分"], payload["当前步长"], payload["累计样本数"]), (60, 2, 1))

    def test_feishu_baseline_chain_not_broken(self):
        all_events = [
            {"fields": {"事件ID": "baseline:ZCode / GLM", "事件类型": "baseline",
                        "模型键": "ZCode / GLM", "评估时间": "2026-09-01T00:00:00+08:00",
                        "评分后": 50, "步长后": 1, "样本数后": 0}},
            {"fields": feishu_fields("a1", "ZCode", "GLM", "轻微问题",
                                     "2026-09-30T10:00:00+08:00",
                                     chain={"评分后": 45, "步长后": 1, "样本数后": 1, "评分变化": -5})},
        ]
        st = feishu.state(all_events, "ZCode / GLM")
        self.assertEqual((st["共享评分"], st["当前步长"], st["累计样本数"]), (45, 1, 1))


class TestUnifiedRejections(unittest.TestCase):
    """统一契约新行为：身份未知不可计分（local 侧补齐）。"""

    def _record(self, path, model, outcome="无问题", confirmed=True):
        import argparse
        ns = argparse.Namespace(
            event_id="x1", agent="ZCode", model=model, task="审查", stage="S",
            evaluated_at="2026-09-30T10:00:00+08:00", outcome=outcome,
            evidence="e", issue=[], identity_confirmed=confirmed)
        with contextlib.redirect_stdout(io.StringIO()):
            local.cmd_record(ns, path)

    def test_unknown_model_confirmed_rejected(self):
        with tempfile.TemporaryDirectory(prefix="bw-rej-") as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            with self.assertRaisesRegex(local.LedgerError, "身份不明确"):
                self._record(path, "未知")

    def test_unknown_model_insufficient_evidence_also_rejected(self):
        with tempfile.TemporaryDirectory(prefix="bw-rej-") as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            with self.assertRaisesRegex(local.LedgerError, "身份不明确"):
                self._record(path, "未知", outcome="证据不足")

    def test_pending_unknown_allowed_then_resolve_requires_identity(self):
        import argparse
        with tempfile.TemporaryDirectory(prefix="bw-rej-") as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            ns = argparse.Namespace(
                event_id="p1", agent="ZCode", model="未知", task="审查", stage="S",
                evaluated_at="2026-09-30T10:00:00+08:00", outcome="无问题",
                evidence="e", issue=[], identity_confirmed=False)
            with contextlib.redirect_stdout(io.StringIO()):
                local.cmd_record(ns, path)  # pending 允许未知
            events = local.read_events(path)
            self.assertEqual(events[0]["type"], "identity_pending")
            # resolve 到未知身份：拒绝
            rns = argparse.Namespace(
                pending_event_id="p1", agent="ZCode", model="未知",
                identity_evidence="x", identity_confirmed=True)
            with self.assertRaisesRegex(local.LedgerError, "身份不明确"):
                with contextlib.redirect_stdout(io.StringIO()):
                    local.cmd_resolve(rns, path)


class TestCliEntryModes(unittest.TestCase):
    def test_both_entry_modes_import_and_run(self):
        env = dict(os.environ, PYTHONUTF8="1")
        with tempfile.TemporaryDirectory(prefix="bw-cli-") as tmp:
            ledger = os.path.join(tmp, "l.jsonl")
            env["BRAIN_WORKER_LEDGER"] = ledger
            for argv in ([sys.executable, str(REPO / "scripts" / "local_ledger.py")],
                         [sys.executable, "-m", "scripts.local_ledger"]):
                proc = subprocess.run(argv + ["status", "--agent", "ZCode", "--model", "GLM"],
                                       capture_output=True, text=True, encoding="utf-8",
                                       env=env, cwd=str(REPO), timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("共享评分", proc.stdout)
            # feishu 双入口：缺配置明确拒绝（导入成功、零网络）
            for argv in ([sys.executable, str(REPO / "scripts" / "capability_ledger.py")],
                         [sys.executable, "-m", "scripts.capability_ledger"]):
                env2 = dict(env)
                for name in feishu.ENV_KEYS.values():
                    env2.pop(name, None)
                env2.pop("BRAIN_WORKER_PLATFORMS_CONFIG", None)
                proc = subprocess.run(argv + ["status", "--agent", "ZCode", "--model", "GLM"],
                                       capture_output=True, text=True, encoding="utf-8",
                                       env=env2, cwd=str(REPO), timeout=60)
                self.assertEqual(proc.returncode, 1)
                self.assertIn("缺少必需飞书配置", proc.stderr)


class TestExactAliasBoundaries05A(unittest.TestCase):
    """BW-DUAL-05A 回归：精确别名、历史碰撞拒绝、单旧对象兼容、指纹重算键。

    以下用例在 05 版（前缀/包含匹配、无碰撞守卫、指纹用存储键）上全部失败。
    """

    def test_version_suffixes_stay_separated(self):
        self.assertNotEqual(core.canonical_model("火山 GLM-5.3-Flash"), core.canonical_model("火山 GLM-5.3"))
        self.assertEqual(core.canonical_model("火山 GLM-5.3-Flash"), "火山 GLM-5.3-Flash")
        for a, b in (("GLM-5.3", "GLM-5.3-Flash"), ("V2.6", "V2.6 Pro"), ("GLM-5.3", "GLM-5.4")):
            self.assertNotEqual(core.lenient_key("ZCode", a), core.lenient_key("ZCode", b), "%s vs %s" % (a, b))

    def test_unlisted_names_not_merged(self):
        # 未列出的应用不做前缀归并；未列出的模型不做包含归并
        self.assertEqual(core.canonical_agent("ZCode Pro Max"), "ZCode Pro Max")
        self.assertEqual(core.canonical_agent("NewDesk"), "NewDesk")
        self.assertEqual(core.canonical_model("火山 GLM-4"), "火山 GLM-4")
        self.assertEqual(core.canonical_model("MiMo V2.6 Pro Max"), "MiMo V2.6 Pro Max")

    def _legacy_event(self, eid, agent, stored_key):
        return {"event_id": eid, "type": "assessment", "agent": agent, "model": "GLM",
                "model_key": stored_key, "task": "审查", "stage": "S1",
                "evaluated_at": "2026-09-28T10:00:00+08:00", "outcome": "无问题",
                "issues": [], "evidence": "e", "identity_confirmed": True,
                "resolved_from": None, "recorded_at": "2026-09-28T10:00:05+08:00"}

    def test_history_collision_rejected_zero_append(self):
        # 两个历史独立存储键映射到同一规范身份：读写全部拒绝，零追加。
        events = [self._legacy_event("a", "ZCode", "ZCode / GLM"),
                  self._legacy_event("b", "ZCode Desktop", "ZCode Desktop / GLM")]
        with self.assertRaisesRegex(local.LedgerError, "身份碰撞"):
            local.status_payload(events, "ZCode", "GLM")
        with tempfile.TemporaryDirectory(prefix="bw-05a-coll-") as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            local_write_events(path, events)
            import argparse
            ns = argparse.Namespace(
                event_id="c", agent="ZCode", model="GLM", task="审查", stage="S3",
                evaluated_at="2026-09-30T12:00:00+08:00", outcome="无问题",
                evidence="e", issue=[], identity_confirmed=True)
            with self.assertRaisesRegex(local.LedgerError, "身份碰撞"):
                with contextlib.redirect_stdout(io.StringIO()):
                    local.cmd_record(ns, path)
            self.assertEqual(len(local.read_events(path)), 2)  # 零追加

    def test_single_legacy_object_read_unchanged_and_retry_idempotent(self):
        # 单一旧别名对象：读分不变（60/2/1）；原样重试（同参数）幂等零追加。
        legacy = self._legacy_event("b", "ZCode Desktop", "ZCode Desktop / GLM")
        payload = local.status_payload([dict(legacy, _seq=1)], "ZCode", "GLM")
        self.assertEqual((payload["共享评分"], payload["当前步长"], payload["累计样本数"]), (60, 2, 1))
        with tempfile.TemporaryDirectory(prefix="bw-05a-leg-") as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            local_write_events(path, [legacy])
            import argparse
            ns = argparse.Namespace(
                event_id="b", agent="ZCode Desktop", model="GLM", task="审查", stage="S1",
                evaluated_at="2026-09-28T10:00:00+08:00", outcome="无问题",
                evidence="e", issue=[], identity_confirmed=True)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = local.cmd_record(ns, path)
            self.assertEqual(rc, 0)
            result = json.loads(out.getvalue())
            self.assertTrue(result["idempotent"])
            self.assertEqual(len(local.read_events(path)), 1)  # 零追加
            self.assertEqual(result["共享评分"], 60)

    def test_conflict_rejected_without_new_id_guidance(self):
        legacy = self._legacy_event("b", "ZCode Desktop", "ZCode Desktop / GLM")
        with tempfile.TemporaryDirectory(prefix="bw-05a-conf-") as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            local_write_events(path, [legacy])
            import argparse
            ns = argparse.Namespace(
                event_id="b", agent="ZCode Desktop", model="GLM", task="审查", stage="S1",
                evaluated_at="2026-09-28T10:00:00+08:00", outcome="无问题",
                evidence="e-CHANGED", issue=[], identity_confirmed=True)
            with self.assertRaisesRegex(local.LedgerError, "内容不一致") as ctx:
                with contextlib.redirect_stdout(io.StringIO()):
                    local.cmd_record(ns, path)
            self.assertNotIn("新的稳定唯一标识", str(ctx.exception))
            self.assertIn("保留原事件 ID", str(ctx.exception))
            self.assertEqual(len(local.read_events(path)), 1)

    def test_changed_outcome_or_model_conflict_rejected(self):
        legacy = self._legacy_event("b", "ZCode Desktop", "ZCode Desktop / GLM")
        import argparse
        base = dict(event_id="b", agent="ZCode Desktop", task="审查", stage="S1",
                    evaluated_at="2026-09-28T10:00:00+08:00", evidence="e",
                    issue=[], identity_confirmed=True)
        for changed in (dict(base, model="GLM-5.3-Flash", outcome="无问题"),
                        dict(base, model="GLM", outcome="重大问题")):
            with tempfile.TemporaryDirectory(prefix="bw-05a-cm-") as tmp:
                path = os.path.join(tmp, "ledger.jsonl")
                local_write_events(path, [legacy])
                with self.assertRaisesRegex(local.LedgerError, "内容不一致"):
                    with contextlib.redirect_stdout(io.StringIO()):
                        local.cmd_record(argparse.Namespace(**changed), path)
                self.assertEqual(len(local.read_events(path)), 1)


class TestFeishuEntryParity05A(unittest.TestCase):
    """BW-DUAL-05A：feishu 真实 main 入口（网络桩）与 local 行为一致。

    不比较共享函数结果——走 main() 全流程（参数解析→canonical→锁→
    token→records→幂等/写入→summary_upsert），验证入口层行为。
    """

    def setUp(self):
        self.events = []
        self.summary = []  # 05C 起 main 在写入前会读取总账表；本类不验证总账
        # 流程（summary_upsert 已桩），故总账表桩返回空，与事件表分开。

        def records(table, _bearer):
            return list(self.summary if table == "tbls" else self.events)

        def request(method, path, _bearer=None, body=None, params=None):
            self.events.append({"fields": body["fields"].copy()})
            return {}

        self.patches = [
            patch.object(feishu, "cfg", lambda: {"app_id": "cli_fake", "app_secret": "s",
                                                 "app_token": "tok", "summary_table": "tbls",
                                                 "events_table": "tble", "legacy_ledger": None}),
            patch.object(feishu, "local_lock", lambda: contextlib.nullcontext()),
            patch.object(feishu, "token", lambda: "fake"),
            patch.object(feishu, "records", records),
            patch.object(feishu, "request", request),
            patch.object(feishu, "summary_upsert", lambda *_a: None),
            patch.object(feishu, "check_legacy_drift", lambda _e: None),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            feishu.main(list(args))
        return json.loads(out.getvalue())

    def pending(self, eid="p1", agent="ZCode", model="未知", outcome="无问题"):
        return self.run_cli("record", "--event-id", eid, "--agent", agent,
                            "--model", model, "--task", "审查", "--stage", "11",
                            "--evaluated-at", "2026-09-30T10:00:00+08:00",
                            "--outcome", outcome, "--evidence", "report.md")

    def resolve(self, model="GLM", eid="p1", agent="ZCode", evidence="user confirmed"):
        return self.run_cli("resolve", "--pending-event-id", eid, "--agent", agent,
                            "--model", model, "--identity-evidence", evidence,
                            "--identity-confirmed")

    def _fields(self, eid, agent, model, outcome, etype="assessment", stored_key=None,
                evaluated_at="2026-09-28T10:00:00+08:00", evidence="e", record_time=None,
                chain=None):
        fields = {"事件ID": eid, "事件类型": etype,
                  "模型键": stored_key if stored_key is not None else core.lenient_key(agent, model),
                  "桌面应用": agent, "模型/版本": model, "任务类型": "审查", "阶段编号": "S1",
                  "评估时间": evaluated_at,
                  "记录时间": record_time or "2026-09-28T10:00:05.%06d+08:00" % len(self.events),
                  "主脑结论": outcome, "证据": evidence}
        if chain:
            fields.update(chain)
        return fields

    def test_pending_resolve_once_and_duplicate_idempotent(self):
        self.pending()
        self.assertEqual(self.events[0]["fields"]["事件类型"], "identity_pending")
        resolved = self.resolve(model="GLM")
        self.assertFalse(resolved["idempotent"])
        self.assertEqual((resolved["共享评分"], resolved["累计样本数"]), (60, 1))
        again = self.resolve(model="GLM")
        self.assertTrue(again["idempotent"])
        self.assertEqual(len([e for e in self.events if e["fields"]["事件ID"] == "resolve:p1"]), 1)

    def test_resolve_different_model_version_rejected(self):
        self.pending()
        self.resolve(model="GLM")
        with self.assertRaisesRegex(RuntimeError, "已归属其他模型|内容不一致"):
            self.resolve(model="GLM-5.3-Flash")  # 不同版本：拒绝重复计分

    def test_resolve_evidence_change_rejected(self):
        self.pending()
        self.resolve(model="GLM")
        with self.assertRaisesRegex(RuntimeError, "内容不一致"):
            self.resolve(model="GLM", evidence="different evidence")

    def test_legacy_stored_key_compat_match_and_retry_idempotent(self):
        # 单一旧存储键（非规范）经重算兼容匹配：读分包含该对象，原样重试幂等零写入。
        self.events.append({"fields": self._fields("b", "ZCode Desktop", "GLM", "无问题",
                                                   stored_key="ZCode Desktop / GLM",
                                                   chain={"评分后": 60, "步长后": 2, "样本数后": 1, "评分变化": 10})})
        st = feishu.state(self.events, "ZCode / GLM")
        self.assertEqual((st["共享评分"], st["累计样本数"]), (60, 1))
        out = self.run_cli("record", "--event-id", "b", "--agent", "ZCode Desktop",
                           "--model", "GLM", "--task", "审查", "--stage", "S1",
                           "--evaluated-at", "2026-09-28T10:00:00+08:00",
                           "--outcome", "无问题", "--evidence", "e", "--identity-confirmed")
        self.assertTrue(out["idempotent"])
        self.assertEqual(len(self.events), 1)  # 零新增

    def test_collision_rejected_before_any_write(self):
        # 两个不同存储键映射同一规范身份：main 入口拒绝，零新增事件（零外部写入）。
        self.events.append({"fields": self._fields("a", "ZCode", "GLM", "无问题",
                                                   stored_key="ZCode / GLM",
                                                   chain={"评分后": 60, "步长后": 2, "样本数后": 1, "评分变化": 10})})
        self.events.append({"fields": self._fields("b", "ZCode Desktop", "GLM", "无问题",
                                                   stored_key="ZCode Desktop / GLM",
                                                   chain={"评分后": 60, "步长后": 2, "样本数后": 1, "评分变化": 10})})
        before = len(self.events)
        with self.assertRaisesRegex(RuntimeError, "身份碰撞"):
            self.run_cli("record", "--event-id", "c", "--agent", "ZCode", "--model", "GLM",
                         "--task", "审查", "--stage", "S3",
                         "--evaluated-at", "2026-09-30T12:00:00+08:00",
                         "--outcome", "无问题", "--evidence", "e", "--identity-confirmed")
        self.assertEqual(len(self.events), before)  # 零新增
        with self.assertRaisesRegex(RuntimeError, "身份碰撞"):
            self.run_cli("status", "--agent", "ZCode", "--model", "GLM")

    def test_record_evidence_change_rejected(self):
        self.events.append({"fields": self._fields("b", "ZCode", "GLM", "无问题")})
        with self.assertRaisesRegex(RuntimeError, "内容不一致|已用于不同内容"):
            self.run_cli("record", "--event-id", "b", "--agent", "ZCode", "--model", "GLM",
                         "--task", "审查", "--stage", "S1",
                         "--evaluated-at", "2026-09-28T10:00:00+08:00",
                         "--outcome", "无问题", "--evidence", "e-CHANGED", "--identity-confirmed")
        self.assertEqual(len(self.events), 1)

    def test_distinct_normalized_identities_stay_separate(self):
        # 版本严格分离：不同模型版本（GLM vs GLM-5.3-Flash）各自独立对象，互不汇总。
        self.events.append({"fields": self._fields("a", "ZCode", "GLM", "无问题",
                                                   stored_key="ZCode / GLM",
                                                   chain={"评分后": 60, "步长后": 2, "样本数后": 1, "评分变化": 10})})
        self.events.append({"fields": self._fields("b", "ZCode", "GLM-5.3-Flash", "轻微问题",
                                                   stored_key="ZCode / GLM-5.3-Flash",
                                                   chain={"评分后": 45, "步长后": 1, "样本数后": 1, "评分变化": -5})})
        st = feishu.state(self.events, "ZCode / GLM")
        self.assertEqual((st["共享评分"], st["累计样本数"]), (60, 1))
        st2 = feishu.state(self.events, "ZCode / GLM-5.3-Flash")
        self.assertEqual((st2["共享评分"], st2["累计样本数"]), (45, 1))


class TestFeishuSingleLegacyLifecycle05B(unittest.TestCase):
    """BW-DUAL-05B：feishu 真实 main 入口，单一历史别名对象不再自造碰撞。

    与 05A 不同：本类**不桩掉 summary_upsert**——用真实的模型总账流程核对
    "不新建重复总账对象"，并统计 request 写调用次数（事件表与总账表分开计数）。
    """

    def setUp(self):
        self.events = []   # 事件表（tble）
        self.summary = []  # 模型总账（tbls）
        self.writes = []   # 所有 POST/PUT (method, table, fields)

        def records(table, _bearer):
            return list(self.summary if table == "tbls" else self.events)

        def request(method, path, _bearer=None, body=None, params=None):
            table = path.split("/tables/")[1].split("/")[0]
            fields = (body or {}).get("fields")
            if method in ("POST", "PUT") and fields is not None:
                self.writes.append((method, table, dict(fields)))
                if table == "tbls":
                    if method == "POST":
                        self.summary.append({"record_id": "s%d" % (len(self.summary) + 1),
                                             "fields": dict(fields)})
                    else:
                        rid = path.rsplit("/", 1)[1]
                        for row in self.summary:
                            if row["record_id"] == rid:
                                row["fields"].update(fields)
                else:
                    self.events.append({"record_id": "e%d" % (len(self.events) + 1),
                                        "fields": dict(fields)})
            return {}

        self.patches = [
            patch.object(feishu, "cfg", lambda: {"app_id": "cli_fake", "app_secret": "s",
                                                 "app_token": "tok", "summary_table": "tbls",
                                                 "events_table": "tble", "legacy_ledger": None}),
            patch.object(feishu, "local_lock", lambda: contextlib.nullcontext()),
            patch.object(feishu, "token", lambda: "fake"),
            patch.object(feishu, "records", records),
            patch.object(feishu, "request", request),
            patch.object(feishu, "check_legacy_drift", lambda _e: None),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            feishu.main(list(args))
        return json.loads(out.getvalue())

    def _legacy_row(self, eid, agent, stored_key, model="GLM", outcome="无问题",
                    etype="assessment", chain=None, record_time="2026-09-28T10:00:05.000000+08:00"):
        fields = {"事件ID": eid, "事件类型": etype, "模型键": stored_key,
                  "桌面应用": agent, "模型/版本": model, "任务类型": "审查", "阶段编号": "S1",
                  "评估时间": "2026-09-28T10:00:00+08:00", "记录时间": record_time,
                  "主脑结论": outcome, "证据": "e"}
        if chain:
            fields.update(chain)
        return {"record_id": "legacy-" + eid, "fields": fields}

    def _event_writes(self):
        return [w for w in self.writes if w[1] == "tble"]

    def test_a_single_legacy_record_status_retry(self):
        self.events.append(self._legacy_row(
            "b", "ZCode Desktop", "ZCode Desktop / GLM",
            chain={"评分后": 60, "步长后": 2, "样本数后": 1, "评分变化": 10}))
        st = self.run_cli("status", "--agent", "ZCode", "--model", "GLM")
        self.assertEqual((st["共享评分"], st["当前步长"], st["累计样本数"]), (60, 2, 1))

        out = self.run_cli("record", "--event-id", "n1", "--agent", "ZCode", "--model", "GLM",
                           "--task", "审查", "--stage", "S2",
                           "--evaluated-at", "2026-09-30T12:00:00+08:00",
                           "--outcome", "无问题", "--evidence", "e", "--identity-confirmed")
        self.assertFalse(out["idempotent"])
        self.assertEqual((out["共享评分"], out["当前步长"], out["累计样本数"]), (70, 3, 2))
        # 事件表全部沿用同一存储键：不产生第二个存储身份（无写入后碰撞）
        self.assertEqual({r["fields"]["模型键"] for r in self.events}, {"ZCode Desktop / GLM"})
        n1 = [r["fields"] for r in self.events if r["fields"]["事件ID"] == "n1"]
        self.assertEqual(len(n1), 1)
        self.assertEqual((n1[0]["评分后"], n1[0]["步长后"], n1[0]["样本数后"]), (70, 3, 2))
        # 模型总账只有一行，键与事件一致
        self.assertEqual(len(self.summary), 1)
        self.assertEqual(self.summary[0]["fields"]["模型键"], "ZCode Desktop / GLM")

        st2 = self.run_cli("status", "--agent", "ZCode", "--model", "GLM")
        self.assertEqual((st2["共享评分"], st2["当前步长"], st2["累计样本数"]), (70, 3, 2))

        before_events, before_summary = len(self.events), len(self.summary)
        again = self.run_cli("record", "--event-id", "n1", "--agent", "ZCode", "--model", "GLM",
                             "--task", "审查", "--stage", "S2",
                             "--evaluated-at", "2026-09-30T12:00:00+08:00",
                             "--outcome", "无问题", "--evidence", "e", "--identity-confirmed")
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(self.events), before_events)     # 零新增事件
        self.assertEqual(len(self.summary), before_summary)   # 总账不重复

    def test_b_pending_resolve_single_identity_no_double_count(self):
        self.run_cli("record", "--event-id", "p1", "--agent", "ZCode", "--model", "未知",
                     "--task", "审查", "--stage", "11",
                     "--evaluated-at", "2026-09-30T10:00:00+08:00",
                     "--outcome", "无问题", "--evidence", "report.md")
        self.assertEqual(self.events[0]["fields"]["事件类型"], "identity_pending")
        r1 = self.run_cli("resolve", "--pending-event-id", "p1", "--agent", "ZCode",
                          "--model", "GLM", "--identity-evidence", "user confirmed",
                          "--identity-confirmed")
        self.assertFalse(r1["idempotent"])
        self.assertEqual((r1["共享评分"], r1["累计样本数"]), (60, 1))
        assessment_keys = {r["fields"]["模型键"] for r in self.events
                           if r["fields"]["事件类型"] == "assessment"}
        self.assertEqual(assessment_keys, {"ZCode / GLM"})
        self.assertEqual(len(self.summary), 1)
        self.assertEqual(self.summary[0]["fields"]["模型键"], "ZCode / GLM")

        r2 = self.run_cli("resolve", "--pending-event-id", "p1", "--agent", "ZCode",
                          "--model", "GLM", "--identity-evidence", "user confirmed",
                          "--identity-confirmed")
        self.assertTrue(r2["idempotent"])
        self.assertEqual(len([r for r in self.events if r["fields"]["事件ID"] == "resolve:p1"]), 1)
        self.assertEqual(len(self.summary), 1)  # 不重复计分、不重复总账

    def test_c_existing_collision_rejected_zero_write_all_paths(self):
        self.events.append(self._legacy_row(
            "a", "ZCode", "ZCode / GLM",
            chain={"评分后": 60, "步长后": 2, "样本数后": 1, "评分变化": 10}))
        self.events.append(self._legacy_row(
            "b", "ZCode Desktop", "ZCode Desktop / GLM",
            chain={"评分后": 60, "步长后": 2, "样本数后": 1, "评分变化": 10}))
        self.events.append(self._legacy_row("p1", "ZCode", "ZCode / GLM", etype="identity_pending",
                                            record_time="2026-09-28T10:00:06.000000+08:00"))
        before = len(self.events)

        with self.assertRaisesRegex(RuntimeError, "身份碰撞"):
            self.run_cli("status", "--agent", "ZCode", "--model", "GLM")
        with self.assertRaisesRegex(RuntimeError, "身份碰撞"):
            self.run_cli("record", "--event-id", "c1", "--agent", "ZCode", "--model", "GLM",
                         "--task", "审查", "--stage", "S3",
                         "--evaluated-at", "2026-09-30T12:00:00+08:00",
                         "--outcome", "无问题", "--evidence", "e", "--identity-confirmed")
        with self.assertRaisesRegex(RuntimeError, "身份碰撞"):
            self.run_cli("resolve", "--pending-event-id", "p1", "--agent", "ZCode",
                         "--model", "GLM", "--identity-evidence", "x", "--identity-confirmed")
        self.assertEqual(len(self.events), before)  # 零新增事件
        self.assertEqual(len(self.writes), 0)       # 零外部写入（含总账）
        self.assertEqual(len(self.summary), 0)

    def test_d_new_canonical_object_created_normally(self):
        out = self.run_cli("record", "--event-id", "d1", "--agent", "ZCode", "--model", "GLM",
                           "--task", "审查", "--stage", "S1",
                           "--evaluated-at", "2026-09-30T12:00:00+08:00",
                           "--outcome", "无问题", "--evidence", "e", "--identity-confirmed")
        self.assertEqual((out["共享评分"], out["累计样本数"]), (60, 1))
        self.assertEqual({r["fields"]["模型键"] for r in self.events}, {"ZCode / GLM"})
        self.assertEqual(len(self.summary), 1)
        self.assertEqual(self.summary[0]["fields"]["模型键"], "ZCode / GLM")

    def test_e_distinct_model_versions_stay_independent(self):
        self.run_cli("record", "--event-id", "v1", "--agent", "ZCode", "--model", "GLM",
                     "--task", "审查", "--stage", "S1",
                     "--evaluated-at", "2026-09-30T12:00:00+08:00",
                     "--outcome", "无问题", "--evidence", "e", "--identity-confirmed")
        self.run_cli("record", "--event-id", "v2", "--agent", "ZCode", "--model", "GLM-5.3-Flash",
                     "--task", "审查", "--stage", "S1",
                     "--evaluated-at", "2026-09-30T13:00:00+08:00",
                     "--outcome", "轻微问题", "--evidence", "e", "--identity-confirmed")
        keys = {r["fields"]["模型键"] for r in self.events}
        self.assertEqual(keys, {"ZCode / GLM", "ZCode / GLM-5.3-Flash"})
        self.assertEqual({r["fields"]["模型键"] for r in self.summary}, keys)  # 各自独立总账
        st = feishu.state(self.events, "ZCode / GLM")
        st2 = feishu.state(self.events, "ZCode / GLM-5.3-Flash")
        self.assertEqual((st["共享评分"], st["累计样本数"]), (60, 1))
        self.assertEqual((st2["共享评分"], st2["累计样本数"]), (45, 1))


class TestFeishuJointIdentityPrecheck05C(unittest.TestCase):
    """BW-DUAL-05C：事件表与模型总账的联合身份预检（首次外部写入前拒绝）。

    走 feishu.main 真实入口 + 虚构表网络桩，**不桩 summary_upsert 与预检**；
    冲突场景逐例核对：原始事件与总账快照逐字段不变、行数、ID、键及
    request 写次数（POST/PUT=0）。与 05B 的差异：05B 只保证"不新建重复
    总账对象"，本类保证"任何身份冲突在首次外部写入之前停止"。
    """

    def setUp(self):
        self.events = []   # 事件表（tble）
        self.summary = []  # 模型总账（tbls）
        self.writes = []   # 所有 POST/PUT (method, table, fields)

        def records(table, _bearer):
            return list(self.summary if table == "tbls" else self.events)

        def request(method, path, _bearer=None, body=None, params=None):
            table = path.split("/tables/")[1].split("/")[0]
            fields = (body or {}).get("fields")
            if method in ("POST", "PUT") and fields is not None:
                self.writes.append((method, table, dict(fields)))
                if table == "tbls":
                    if method == "POST":
                        self.summary.append({"record_id": "s%d" % (len(self.summary) + 1),
                                             "fields": dict(fields)})
                    else:
                        rid = path.rsplit("/", 1)[1]
                        for row in self.summary:
                            if row["record_id"] == rid:
                                row["fields"].update(fields)
                else:
                    self.events.append({"record_id": "e%d" % (len(self.events) + 1),
                                        "fields": dict(fields)})
            return {}

        self.patches = [
            patch.object(feishu, "cfg", lambda: {"app_id": "cli_fake", "app_secret": "s",
                                                 "app_token": "tok", "summary_table": "tbls",
                                                 "events_table": "tble", "legacy_ledger": None}),
            patch.object(feishu, "local_lock", lambda: contextlib.nullcontext()),
            patch.object(feishu, "token", lambda: "fake"),
            patch.object(feishu, "records", records),
            patch.object(feishu, "request", request),
            patch.object(feishu, "check_legacy_drift", lambda _e: None),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            feishu.main(list(args))
        return json.loads(out.getvalue())

    def _legacy_row(self, eid, agent, stored_key, model="GLM", outcome="无问题",
                    etype="assessment", chain=None, record_time="2026-09-28T10:00:05.000000+08:00"):
        fields = {"事件ID": eid, "事件类型": etype, "模型键": stored_key,
                  "桌面应用": agent, "模型/版本": model, "任务类型": "审查", "阶段编号": "S1",
                  "评估时间": "2026-09-28T10:00:00+08:00", "记录时间": record_time,
                  "主脑结论": outcome, "证据": "e"}
        if chain:
            fields.update(chain)
        return {"record_id": "legacy-" + eid, "fields": fields}

    def _summary_row(self, key, agent, model, score=60, step=2, samples=1):
        self._sum_seq = getattr(self, "_sum_seq", 0) + 1
        return {"record_id": "sum-%d" % self._sum_seq, "fields": {
            "模型键": key, "桌面应用": agent, "模型/版本": model,
            "共享评分": score, "当前步长": step, "累计样本数": samples,
            "能力等级": "可用但需逐阶段审查（试用中）", "最近评估时间": ""}}

    def _record(self, eid, agent="ZCode", model="GLM"):
        return self.run_cli("record", "--event-id", eid, "--agent", agent, "--model", model,
                            "--task", "审查", "--stage", "S9",
                            "--evaluated-at", "2026-09-30T12:00:00+08:00",
                            "--outcome", "无问题", "--evidence", "e", "--identity-confirmed")

    def _snapshot(self):
        return copy.deepcopy(self.events), copy.deepcopy(self.summary)

    def _assert_zero_write(self, snapshot):
        events, summary = snapshot
        self.assertEqual(self.events, events)       # 事件表逐字段不变
        self.assertEqual(self.summary, summary)     # 总账逐字段不变
        self.assertEqual(self.writes, [])           # POST/PUT 次数=0

    def test_a_two_tables_different_keys_rejected_zero_write(self):
        # 主脑复现场景：事件表旧键与总账键不同（同一规范身份）→ 写前拒绝。
        self.events.append(self._legacy_row(
            "b", "ZCode Desktop", "ZCode Desktop / GLM",
            chain={"评分后": 60, "步长后": 2, "样本数后": 1, "评分变化": 10}))
        self.summary.append(self._summary_row("ZCode / GLM", "ZCode", "GLM"))
        snap = self._snapshot()
        with self.assertRaisesRegex(RuntimeError, "两表键不一致"):
            self._record("n1")
        self._assert_zero_write(snap)
        # 同一冲突态下：同 ID 幂等重试与 resolve 也写前拒绝
        with self.assertRaisesRegex(RuntimeError, "两表键不一致"):
            self.run_cli("record", "--event-id", "b", "--agent", "ZCode Desktop",
                         "--model", "GLM", "--task", "审查", "--stage", "S1",
                         "--evaluated-at", "2026-09-28T10:00:00+08:00",
                         "--outcome", "无问题", "--evidence", "e", "--identity-confirmed")
        self.events.append(self._legacy_row("p1", "ZCode Desktop", "ZCode Desktop / GLM",
                                            etype="identity_pending",
                                            record_time="2026-09-28T10:00:06.000000+08:00"))
        snap2 = self._snapshot()
        with self.assertRaisesRegex(RuntimeError, "两表键不一致"):
            self.run_cli("resolve", "--pending-event-id", "p1", "--agent", "ZCode",
                         "--model", "GLM", "--identity-evidence", "x", "--identity-confirmed")
        self._assert_zero_write(snap2)

    def test_b_duplicate_summary_rows_rejected_all_paths_zero_write(self):
        # 同一规范身份两个总账行：record / 同 ID 重试 / resolve 全部写前拒绝。
        self.events.append(self._legacy_row(
            "a", "ZCode", "ZCode / GLM",
            chain={"评分后": 60, "步长后": 2, "样本数后": 1, "评分变化": 10}))
        self.summary.append(self._summary_row("ZCode / GLM", "ZCode", "GLM"))
        self.summary.append(self._summary_row("ZCode Desktop / GLM", "ZCode Desktop", "GLM"))
        with self.assertRaisesRegex(RuntimeError, "总账存在多行"):
            self._record("n1")
        with self.assertRaisesRegex(RuntimeError, "总账存在多行"):
            self._record("a")
        self.events.append(self._legacy_row("p1", "ZCode", "ZCode / GLM",
                                            etype="identity_pending",
                                            record_time="2026-09-28T10:00:06.000000+08:00"))
        snap = self._snapshot()
        with self.assertRaisesRegex(RuntimeError, "总账存在多行"):
            self.run_cli("resolve", "--pending-event-id", "p1", "--agent", "ZCode",
                         "--model", "GLM", "--identity-evidence", "x", "--identity-confirmed")
        self._assert_zero_write(snap)

    def test_c_summary_identity_contradicts_key_zero_write(self):
        # 总账行占用计划存储键但身份字段指向其他身份（GLM-4）：零写入拒绝。
        self.events.append(self._legacy_row(
            "a", "ZCode", "ZCode / GLM",
            chain={"评分后": 60, "步长后": 2, "样本数后": 1, "评分变化": 10}))
        self.summary.append(self._summary_row("ZCode / GLM", "ZCode", "GLM-4"))
        snap = self._snapshot()
        with self.assertRaisesRegex(RuntimeError, "身份矛盾"):
            self._record("n1")
        self._assert_zero_write(snap)

    def test_d_same_legacy_key_both_tables_normal_lifecycle(self):
        # 两表同一旧存储键：新增评价、status、同 ID 重试正常，总账始终一行。
        self.events.append(self._legacy_row(
            "a", "ZCode Desktop", "ZCode Desktop / GLM",
            chain={"评分后": 60, "步长后": 2, "样本数后": 1, "评分变化": 10}))
        self.summary.append(self._summary_row("ZCode Desktop / GLM", "ZCode", "GLM"))
        out = self._record("n1")
        self.assertFalse(out["idempotent"])
        self.assertEqual((out["共享评分"], out["当前步长"], out["累计样本数"]), (70, 3, 2))
        self.assertEqual({r["fields"]["模型键"] for r in self.events}, {"ZCode Desktop / GLM"})
        self.assertEqual(len(self.summary), 1)
        self.assertEqual(self.summary[0]["fields"]["模型键"], "ZCode Desktop / GLM")
        self.assertEqual(self.summary[0]["fields"]["共享评分"], 70)
        st = self.run_cli("status", "--agent", "ZCode", "--model", "GLM")
        self.assertEqual((st["共享评分"], st["累计样本数"]), (70, 2))
        again = self._record("n1")
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(self.summary), 1)   # 总账始终一行

    def test_e_single_legacy_event_empty_summary_creates_one_row(self):
        # 单一旧事件对象 + 空总账：正常创建一行总账，不新增事件存储身份。
        self.events.append(self._legacy_row(
            "a", "ZCode Desktop", "ZCode Desktop / GLM",
            chain={"评分后": 60, "步长后": 2, "样本数后": 1, "评分变化": 10}))
        out = self._record("n1")
        self.assertEqual((out["共享评分"], out["累计样本数"]), (70, 2))
        self.assertEqual(len(self.summary), 1)
        self.assertEqual(self.summary[0]["fields"]["模型键"], "ZCode Desktop / GLM")
        self.assertEqual({r["fields"]["模型键"] for r in self.events}, {"ZCode Desktop / GLM"})

    def test_f_brand_new_object_created_normally(self):
        # 全新对象：事件表与总账均为空 → 正常创建，不误判冲突。
        out = self._record("n1")
        self.assertEqual((out["共享评分"], out["当前步长"], out["累计样本数"]), (60, 2, 1))
        self.assertEqual({r["fields"]["模型键"] for r in self.events}, {"ZCode / GLM"})
        self.assertEqual(len(self.summary), 1)
        self.assertEqual(self.summary[0]["fields"]["模型键"], "ZCode / GLM")

    def test_g_summary_without_event_chain_rejected_no_overwrite(self):
        # 仅有总账、缺事件链：明确停止，不新建基线覆盖已有评分。
        self.summary.append(self._summary_row("ZCode / GLM", "ZCode", "GLM", score=60, step=2, samples=1))
        snap = self._snapshot()
        with self.assertRaisesRegex(RuntimeError, "证据缺口"):
            self._record("n1")
        self._assert_zero_write(snap)
        self.assertEqual(self.summary[0]["fields"]["共享评分"], 60)  # 未被覆盖


if __name__ == "__main__":
    unittest.main(verbosity=2)
