#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""brain-worker 台账共享核心（BW-DUAL-05）。

纯 Python 标准库、无网络、无文件读写。local（local_ledger.py）与
feishu（capability_ledger.py）后端共用同一套评分/步长/等级/身份归一/
幂等指纹规则；存储、锁、配置与 API 各自适配，事件文件/表格格式互不相同。

统一语义契约（两后端一致）：
  - 评分转换：无问题(+10,+1) / 轻微问题(-5,-1) / 重大问题(-15,-2) /
    严重问题(-30, 步长归 1) / 证据不足(±0, 样本不变)。评分 clamp 0..100、
    步长 clamp 1..5。
  - 等级：>=80 且样本>=3 且最近 3 次评估无重大/严重 → 推荐；>=60 可用但
    需逐阶段审查；>=40 观察；否则受限或暂不推荐；样本<3 追加（试用中）。
  - 身份：身份未知不可计分（identity_pending 不参与计分）；resolve 以
    "resolve:<pending-id>" 补计一次；桌面应用归一后不匹配则拒绝归因。
  - 名称归一：仅对下方明确列出的精确等价别名做整串替换；不做前缀/包含
    等会吞掉版本后缀的模糊匹配，未知应用/模型名与未列出的版本保持独立
    身份。历史事件不改写；local 匹配按事件 agent/model 用同一规则重算，
    单一历史存储键可兼容匹配；当多个历史存储键映射到同一规范身份时，
    各后端必须明确拒绝相关读写并报告碰撞（禁止静默汇总/迁移/重算/改写）。
  - 写入规划（plan_stored_key）：规范身份下的存储键（模型键）由写前规划
    决定，两后端一致——既有 0 个 → 用规范键；恰好 1 个（可能是旧别名键）
    → 沿用该既有键，使单一历史对象的后续事件保持同一存储身份，规范键只
    用于匹配，不制造第二个存储键、不改写旧事件；既有多个 → 真实碰撞，在
    任何追加/外部写入前明确拒绝（零副作用）。
  - 幂等指纹：event_id/type/model_key/task/stage/evaluated_at/outcome/
    evidence/issues(排序后比较，顺序不敏感)。model_key 统一取"按事件自身
    agent/model 重算的规范键"（新旧事件同一规则，历史存储键差异不影响
    原样重试的幂等）；recorded_at、存储行号等生成字段不参与；
    identity_confirmed/resolved_from 的实质由 type 与 event_id 蕴含
    （identity_pending 与 resolve: 前缀），不单独参与指纹。同一 event-id
    内容冲突时保留原 ID，核对原事件后修正重试内容或走补证流程；不得换
    新 ID 重复计分。
"""


class LedgerCoreError(ValueError):
    """核心规则违反（未知结论、身份不明确等）。"""


OUTCOMES = {
    "无问题": (10, 1),
    "轻微问题": (-5, -1),
    "重大问题": (-15, -2),
    "严重问题": (-30, None),
    "证据不足": (0, 0),
}

INIT_SCORE = 50
INIT_STEP = 1
INIT_SAMPLES = 0
SCORE_MIN, SCORE_MAX = 0, 100
STEP_MIN, STEP_MAX = 1, 5
RECOMMEND_RECENT_BAN = ("重大问题", "严重问题")

# 精确等价别名表（BW-DUAL-05A）：应用/模型名与别名整串相等时才替换为
# 规范名。不做前缀/包含匹配——那会吞掉版本后缀（如“火山 GLM-5.3-Flash”
# 被并入“火山 GLM-5.3”）；未列出的名称与未知版本保持独立身份。
AGENT_EXACT_ALIASES = {
    "ZCode Desktop": "ZCode",
    "Xiaomi MiMo 浏览器": "Xiaomi MiMo",
}
MODEL_EXACT_ALIASES = {
    "官方 GLM-5.3-Flash": "ZCode 官方 GLM-5.3-Flash",
    "kimi-k2.8-preview": "火山 kimi-k2.8-preview",
}


def canonical_agent(agent):
    agent = (agent or "").strip()
    return AGENT_EXACT_ALIASES.get(agent, agent)


def canonical_model(model):
    model = (model or "").strip()
    return MODEL_EXACT_ALIASES.get(model, model)


def lenient_key(agent, model):
    """归一 agent/model 后拼键；不做身份校验、不替换未知身份。

    用于事件存储与匹配（含 identity_pending 事件的显示键）——历史事件
    不迁移，匹配时按事件 agent/model 用本函数重算，归一后相同才合并。
    """
    return "%s / %s" % (canonical_agent(agent), canonical_model(model))


def collision_message(key, stored):
    """历史碰撞的统一提示文案（两后端共用，避免文案漂移）。"""
    return (
        "历史事件身份碰撞：规范键 “%s” 下存在多个历史存储键（%s）。"
        "拒绝汇总/迁移/改写历史评分；请人工核对这些事件的归属后处理，"
        "在此之前该模型的所有读写停止。" % (key, "、".join(sorted(stored)))
    )


def plan_stored_key(canonical_key, existing_stored_keys):
    """写前规划新事件应使用的存储键（两后端共用，须在任何写入前调用）。

    existing_stored_keys：同一规范身份下已存在的存储键（模型键）集合。
      - 0 个 → 使用规范键（新规范对象正常创建）；
      - 1 个 → 沿用该既有存储键：单一历史别名对象（如旧版直拼键）的后续
        事件保持同一存储身份，规范键只用于匹配；不制造第二个存储键，
        也不改写历史事件；
      - 多个 → 真实历史碰撞，抛 LedgerCoreError，调用方须零写入直接拒绝。
    """
    keys = {k for k in existing_stored_keys if k}
    if len(keys) > 1:
        raise LedgerCoreError(collision_message(canonical_key, keys))
    if len(keys) == 1:
        return next(iter(keys))
    return canonical_key


def canonical_triple(agent, model, allow_pending=False):
    """严格身份归一：返回 (agent, model, key)；身份不明确时抛错。

    allow_pending=True 时把空/未确认/未知模型替换为“待确认模型”；
    allow_pending=False 时遇到这些值抛 LedgerCoreError（身份未知不可计分）。
    """
    agent = canonical_agent(agent)
    model = (model or "").strip()
    if allow_pending and (not model or "未确认" in model or "未知" in model):
        model = "待确认模型"
    model = canonical_model(model)
    if (not agent or not model or "未确认" in model or "未知" in model
            or (model == "待确认模型" and not allow_pending)):
        raise LedgerCoreError("Agent 或模型身份不明确，不能合并评分")
    return agent, model, "%s / %s" % (agent, model)


def transition(score, step, samples, outcome):
    """评分/步长/样本转换；返回 (评分, 步长, 样本, 本次评分变化)。"""
    if outcome not in OUTCOMES:
        raise LedgerCoreError("未知评分结论：%s" % outcome)
    delta, step_delta = OUTCOMES[outcome]
    if score is None:
        if outcome == "证据不足":
            return None, step, samples, 0
        score = INIT_SCORE
    new_score = max(SCORE_MIN, min(SCORE_MAX, score + delta))
    base = INIT_STEP if step is None else step
    new_step = STEP_MIN if step_delta is None else max(STEP_MIN, min(STEP_MAX, base + step_delta))
    new_samples = samples + (0 if outcome == "证据不足" else 1)
    return new_score, new_step, new_samples, delta


def level(score, samples, recent=()):
    if score is None:
        return "未定级"
    recommended = (score >= 80 and samples >= 3 and len(recent) >= 3
                   and not set(RECOMMEND_RECENT_BAN).intersection(recent))
    name = ("推荐" if recommended else "可用但需逐阶段审查" if score >= 60
            else "观察" if score >= 40 else "受限或暂不推荐")
    return name + ("（试用中）" if samples < 3 else "")


def resolve_id(pending_event_id):
    return "resolve:%s" % pending_event_id


FINGERPRINT_FIELDS = ("event_id", "type", "model_key", "task", "stage",
                      "evaluated_at", "outcome", "evidence", "issues")


def idempotency_fingerprint(event):
    """幂等指纹：比较会改变阶段评价实质的字段。

    event 为核心规范 dict（键见 FINGERPRINT_FIELDS；model_key 为归一后键）。
    issues 统一转列表并排序后比较（顺序不敏感）；生成字段不参与。
    """
    issues = event.get("issues") or []
    if isinstance(issues, str):
        issues = [issues] if issues.strip() else []
    return (
        event.get("event_id"),
        event.get("type"),
        event.get("model_key"),
        event.get("task"),
        event.get("stage"),
        event.get("evaluated_at"),
        event.get("outcome"),
        event.get("evidence"),
        tuple(sorted(issues)),
    )
