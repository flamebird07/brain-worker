"""prompt_contract — 三入口共享的九节报告可校验契约（纯标准库，无业务依赖）。

单一来源：精确九标题、首末标记、阶段/项目字段与结语在此定义，三个执行入口在
实际调用模型前使用本模块的返回值构造发送载荷，并把发送载荷按字节留证回读。

设计边界（与 source-review 对技能升级的诉求一致）：
- 只搬运/记录契约与载荷证据，不裁剪原始报告、不替模型写报告、不改分析器绑定强度；
- 标题严格等同 qoder_direct.SECTION_HEADERS；本模块不导入 qoder_direct/codebuddy/zcode，
  避免循环导入（由它们反向导入本模块）；
- prompt_sha256 一律指原始提示词文件字节哈希（dispatch-plan 三入口统一此口径）；
  实际任务文本 UTF-8、完整发送载荷、契约各自单独记录哈希，绝不把 JSON 文件本身哈希
  冒充 prompt 载荷哈希。
"""
import hashlib

REPORT_START = 'WORKER_REPORT_START'
REPORT_END = 'WORKER_REPORT_END'
CLOSING_LINE = '本阶段汇报结束；等待主脑验收。'
STAGE_FIELD_NAMES = ('阶段编号', '阶段编号与执行方式')
PATH_FIELD_NAMES = ('实际项目绝对路径',)
# 与 qoder_direct.SECTION_HEADERS 逐字相同；测试断言二者相等，防止标题漂移。
SECTION_HEADERS = ('一、当前基线与授权', '二、实际执行范围', '三、已验证事实',
                   '四、推断（必须与事实分开）', '五、测试与验证',
                   '六、未完成项与剩余风险', '七、实际副作用与越界检查',
                   '八、本阶段状态', '九、建议下一步（只提出建议，不执行）')

# 九节骨架：首末标记独占行、阶段/路径/汇报时间字段同行、九标题顺序、结语。
# 作为契约正文随发送载荷到达模型，不替模型填充业务内容。
NINE_SECTION_TEMPLATE = '\n'.join([
    REPORT_START,
    '阶段编号与执行方式：',
    '实际项目绝对路径：',
    '汇报时间与执行环境：',
    '',
    SECTION_HEADERS[0], '- 接手时已验证的文件/版本/运行状态：',
    '- 本阶段获得的授权范围：', '- 本阶段禁止事项：',
    '- 交接来源（如更换 Agent）：', '',
    SECTION_HEADERS[1], '- 实际执行的动作：', '- 变更文件及范围：',
    '- 未修改但检查过的关键文件或状态：', '',
    SECTION_HEADERS[2], '1. 事实：', '   证据位置/命令/回读：', '   验证环境：',
    '2. 事实：', '   证据位置/命令/回读：', '   验证环境：', '',
    SECTION_HEADERS[3], '- 推断及依据：', '- 仍未知的内容：', '',
    SECTION_HEADERS[4], '- 工作目录：', '- 原样命令：', '- 实际退出码：',
    '- 关键结果：', '- 未运行或未完成的验证及原因：', '',
    SECTION_HEADERS[5], '- 未完成项：', '- 失败项：', '- 缺失证据：',
    '- 剩余风险：', '',
    SECTION_HEADERS[6], '- 实际副作用：', '- 越界动作：',
    '- 敏感信息处理：仅写脱敏摘要，不写 Cookie、密钥、令牌或账务原文。', '',
    SECTION_HEADERS[7],
    '- 状态（只能选一项）：阶段完成 / 部分完成 / 待补验证 / 阻塞 / 异常',
    '- 状态依据：', '- 是否满足本阶段验收标准：是 / 否 / 证据不足', '',
    SECTION_HEADERS[8], '- 建议：', '',
    CLOSING_LINE,
    REPORT_END,
])

_CONTRACT_LEAD = ('Final response must contain only the complete nine-section report. '
                  'First line: {start}. Last line: {end}. '
                  'Those markers must appear exactly once each; never quote them in the body. '
                  'No preface, epilogue, or code fences. Keep field names and values on the same line: '
                  '阶段编号与执行方式：{stage}；{mode}。 '
                  '实际项目绝对路径：{workspace}。 Use the exact path without punctuation in its field.')


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_contract(stage: str, workspace: str, mode: str = 'direct') -> str:
    """构造完整契约文本：精确英文指令（含阶段/路径具体值）+ 九节骨架。

    以英文句子开头、阶段 token 与绝对路径都逐字出现在返回值里；调用方把返回值原样
    放入实际发送载荷（Qoder 走 --append-system-prompt argv 通道；CodeBuddy/ZCode
    拼接到任务）。本函数是纯字符串构造，不读文件、不起子进程。"""
    if not stage or not str(stage).strip():
        raise ValueError('build_contract requires a non-empty stage')
    if not workspace or not str(workspace).strip():
        raise ValueError('build_contract requires a non-empty workspace path')
    lead = _CONTRACT_LEAD.format(start=REPORT_START, end=REPORT_END, stage=stage,
                                 mode=mode, workspace=workspace)
    return (lead + '\nReproduce this nine-section contract verbatim as the report '
            'skeleton (fill every field on its line; write 无/未运行 for empty fields, '
            'never omit a section):\n' + NINE_SECTION_TEMPLATE)


def compose_task_payload(contract: str, task_text: str) -> str:
    """完整发送任务载荷 = 契约 + 空行 + 原任务文本（逐字保留、不裁剪）。"""
    return contract + '\n\n' + task_text


def payload_evidence(*, raw_prompt_bytes: bytes, sent_task_text: str,
                     contract, sent_payload_bytes: bytes,
                     newline_caliber: str) -> dict:
    """把“原始文件字节 / 实际任务文本 / 契约 / 完整载荷”分别留哈希，并给出换行口径。

    prompt_sha256 恒为原始提示词文件字节哈希（dispatch-plan 统一口径）；sent_payload_sha256
    为真正传给子进程/SDK 的完整载荷字节哈希，二者在 CRLF+归一发送的场景可不同且都留证。
    contract=None 表示该通道未附带契约（如纯预检），相应哈希记为 None 而非空串。"""
    contract_bytes = (contract.encode('utf-8') if isinstance(contract, str) else None)
    raw_crlf = b'\r\n' in raw_prompt_bytes
    sent_crlf = b'\r\n' in sent_payload_bytes
    return {
        'prompt_sha256': sha256_hex(raw_prompt_bytes),
        'original_file_bytes': len(raw_prompt_bytes),
        'task_text_utf8_sha256': sha256_hex(sent_task_text.encode('utf-8')),
        'contract_sha256': (sha256_hex(contract_bytes) if contract_bytes is not None else None),
        'sent_payload_sha256': sha256_hex(sent_payload_bytes),
        'sent_payload_bytes': len(sent_payload_bytes),
        'newline_conversion': {
            'caliber': newline_caliber,
            'raw_file_has_crlf': raw_crlf,
            'sent_payload_has_crlf': sent_crlf,
            'trailing_newline_raw': raw_prompt_bytes.endswith(b'\n'),
            'trailing_newline_sent': sent_payload_bytes.endswith(b'\n'),
            'raw_equals_sent_task_utf8': raw_prompt_bytes == sent_task_text.encode('utf-8'),
        },
    }
