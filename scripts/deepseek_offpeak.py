"""deepseek_offpeak — claude:deepseek-flash 派工时段的离线判定（纯标准库）。

客户口径（北京时间固定 UTC+8，绝不读宿主本地时区）：
- 周一至周五，除去中国法定节假日：09:00（含）–12:00（不含）、14:00（含）–18:00（不含）
  为高峰，高峰期间不得新派 claude:deepseek-flash；其余时段、周末、法定节假日全天可派。
- 周六/周日按官方定义全天空闲，绝不把“调休上班的周六”当高峰（周末判定只看星期几）。
- 年度放假数据以官方公布为准，可轻量成表；只内置已核对的 2026 年安排。
- 未知年度：无法确认某个“工作日高峰时刻”是否落在假日里，因此对 weekday 高峰时段一律保守
  关闭并给出明确原因；offhours 与 weekend 与年份无关，照常放行。绝不臆测未知年度为假日。

本模块只做纯时间/日期判定，不起进程、不联网、不读密钥，供 dispatch_pool 的 claude 时段门
与各离线测试复用同一口径。
"""
from datetime import date, datetime, timedelta, timezone

BEIJING_UTC_OFFSET = timedelta(hours=8)

# 高峰区间（自 00:00 起算的分钟，左闭右开）：09:00–12:00、14:00–18:00。
PEAK_INTERVALS = ((9 * 60, 12 * 60), (14 * 60, 18 * 60))

# 已核对的 2026 年中国法定节假日安排（含首尾，闭区间）。来源见任务书官方公报与北京市政务
# 服务来源；本表为离线最小数据，不含网络日历依赖。
HOLIDAY_RANGES = {
    2026: (
        ((2026, 1, 1), (2026, 1, 3)),
        ((2026, 2, 15), (2026, 2, 23)),
        ((2026, 4, 4), (2026, 4, 6)),
        ((2026, 5, 1), (2026, 5, 5)),
        ((2026, 6, 19), (2026, 6, 21)),
        ((2026, 9, 25), (2026, 9, 27)),
        ((2026, 10, 1), (2026, 10, 7)),
    ),
}

WEEKEND_WEEKDAYS = (5, 6)  # Sat, Sun —— 只看星期几，调休周六同样视为空闲


def known_years():
    return tuple(sorted(HOLIDAY_RANGES))


def to_beijing(now):
    """把注入的 now 归一到北京时间 (UTC+8) 的 naive datetime。

    now=None → 当前 UTC；带 tzinfo → 先转 UTC 再 +8；naive → 按 UTC 理解（与 dispatch_pool
    的 _beijing_hour 口径一致，绝不读取宿主本地时区）。"""
    if now is None:
        now = datetime.now(timezone.utc)
    if isinstance(now, date) and not isinstance(now, datetime):
        base = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    elif now.tzinfo is None:
        base = now.replace(tzinfo=timezone.utc)
    else:
        base = now.astimezone(timezone.utc)
    return (base + BEIJING_UTC_OFFSET).replace(tzinfo=None)


def _minutes_of_day(dt):
    return dt.hour * 60 + dt.minute


def in_peak_minutes(dt):
    """北京时间 dt 是否落在高峰区间（左闭右开，忽略秒；09:00/14:00 含、12:00/18:00 不含）。"""
    m = _minutes_of_day(dt)
    return any(lo <= m < hi for lo, hi in PEAK_INTERVALS)


def is_weekend(d):
    dd = d.date() if isinstance(d, datetime) else d
    return dd.weekday() in WEEKEND_WEEKDAYS


def year_known(d):
    dd = d.date() if isinstance(d, datetime) else d
    return dd.year in HOLIDAY_RANGES


def is_holiday(d):
    """仅当该日期年份有内置放假表且落在任一闭区间内才为 True；未知年份一律 False（不猜）。"""
    dd = d.date() if isinstance(d, datetime) else d
    ranges = HOLIDAY_RANGES.get(dd.year)
    if not ranges:
        return False
    for (sy, sm, sd), (ey, em, ed) in ranges:
        if date(sy, sm, sd) <= dd <= date(ey, em, ed):
            return True
    return False


def classify_dispatch(now=None):
    """返回 {allow, category, reason}。category ∈
    {weekend, holiday, weekday_offpeak, weekday_peak, weekday_peak_unknown_year}。

    判定顺序：周末 → 假日 → 工作日（再分高峰/非高峰）。未知年度的工作日高峰保守关闭。"""
    bj = to_beijing(now)
    d = bj.date()
    if is_weekend(d):
        return {'allow': True, 'category': 'weekend',
                'reason': 'weekend: Beijing official caliber treats Sat/Sun as all-day idle'}
    if year_known(d):
        if is_holiday(d):
            return {'allow': True, 'category': 'holiday',
                    'reason': f'{d.isoformat()} is in the {d.year} statutory holiday table'}
        if in_peak_minutes(bj):
            return {'allow': False, 'category': 'weekday_peak',
                    'reason': 'weekday peak window 09:00-12:00 or 14:00-18:00 (Beijing): '
                              'no new dispatch to claude:deepseek-flash'}
        return {'allow': True, 'category': 'weekday_offpeak',
                'reason': 'weekday off-peak hours (before 09:00, 12:00-14:00, after 18:00 Beijing)'}
    # 未知年度：无法确认工作日高峰是否实为假日，保守关闭高峰；offhours/weekend 仍放行。
    if in_peak_minutes(bj):
        return {'allow': False, 'category': 'weekday_peak_unknown_year',
                'reason': f'year {d.year} holiday table unavailable: cannot confirm a weekday '
                          'peak slot is not a statutory holiday, so peak dispatch is closed '
                          'conservatively (off-hours and weekends remain open)'}
    return {'allow': True, 'category': 'weekday_offpeak',
            'reason': 'weekday off-peak hours (year-independent); dispatch allowed'}


def can_dispatch(now=None) -> bool:
    return classify_dispatch(now)['allow']


if __name__ == '__main__':  # 极简自检：打印当前北京时间判定（不代表真实派工决策）
    import json
    print(json.dumps(classify_dispatch(), ensure_ascii=False, indent=2))
