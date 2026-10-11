"""BW-CLAUDE-DEEPSEEK-S2 离线回归：deepseek_offpeak 时段/假日判定（纯函数、零副作用）。

覆盖：09:00 含 / 12:00 不含 / 14:00 含 / 18:00 不含边界；weekday 高峰关闭、offhours 放行；
weekend（含调休周六，只看星期几）全天放行；2026 法定节假日高峰放行；未知年度工作日高峰保守
关闭、offhours/weekend 放行；固定 UTC+8（naive 按 UTC、aware 先转 UTC）。全部本地、不联网、
不起进程、不读密钥。
"""
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import deepseek_offpeak as off  # noqa: E402

UTC = timezone.utc


def bj(year, month, day, hour, minute=0):
    """按北京时间构造（内部转成对应 UTC aware datetime 注入被测函数）。"""
    # 北京时间 = UTC+8 → UTC = 北京 - 8h。直接构造 naive 北京时间再交给 to_beijing 的
    # naive=UTC 分支不行，这里显式构造 UTC。
    dt_beijing_naive = datetime(year, month, day, hour, minute)
    dt_utc_naive = dt_beijing_naive - off.BEIJING_UTC_OFFSET
    return dt_utc_naive.replace(tzinfo=UTC)


class BoundaryPeakWindows(unittest.TestCase):
    # 2026-10-12 为周一（普通工作日，非 2026 假日表内）
    def test_0900_inclusive_is_peak(self):
        r = off.classify_dispatch(bj(2026, 10, 12, 9, 0))
        self.assertEqual(r['category'], 'weekday_peak')
        self.assertFalse(r['allow'])

    def test_0859_is_offpeak(self):
        r = off.classify_dispatch(bj(2026, 10, 12, 8, 59))
        self.assertEqual(r['category'], 'weekday_offpeak')
        self.assertTrue(r['allow'])

    def test_1159_is_peak(self):
        r = off.classify_dispatch(bj(2026, 10, 12, 11, 59))
        self.assertFalse(r['allow'])

    def test_1200_exclusive_is_offpeak(self):
        r = off.classify_dispatch(bj(2026, 10, 12, 12, 0))
        self.assertEqual(r['category'], 'weekday_offpeak')
        self.assertTrue(r['allow'])

    def test_1400_inclusive_is_peak(self):
        r = off.classify_dispatch(bj(2026, 10, 12, 14, 0))
        self.assertEqual(r['category'], 'weekday_peak')
        self.assertFalse(r['allow'])

    def test_1759_is_peak(self):
        self.assertFalse(off.can_dispatch(bj(2026, 10, 12, 17, 59)))

    def test_1800_exclusive_is_offpeak(self):
        r = off.classify_dispatch(bj(2026, 10, 12, 18, 0))
        self.assertEqual(r['category'], 'weekday_offpeak')
        self.assertTrue(r['allow'])

    def test_midday_gap_offpeak(self):
        self.assertTrue(off.can_dispatch(bj(2026, 10, 12, 13, 0)))

    def test_evening_offpeak(self):
        self.assertTrue(off.can_dispatch(bj(2026, 10, 12, 20, 0)))


class WeekendAndMakeup(unittest.TestCase):
    def test_saturday_peak_is_idle(self):
        # 2026-10-10 周六；即便落在高峰分钟，周末优先判定为空闲
        r = off.classify_dispatch(bj(2026, 10, 10, 10, 0))
        self.assertEqual(r['category'], 'weekend')
        self.assertTrue(r['allow'])

    def test_sunday_idle(self):
        r = off.classify_dispatch(bj(2026, 10, 11, 15, 0))
        self.assertEqual(r['category'], 'weekend')
        self.assertTrue(r['allow'])

    def test_makeup_saturday_never_peak(self):
        # “调休上班的周六”仍按星期几归为周末——绝不当高峰：断言 weekday 决定一切
        self.assertTrue(off.is_weekend(date(2026, 10, 10)))


class HolidayOverridesPeak(unittest.TestCase):
    def test_national_day_peak_allowed(self):
        # 2026-10-01 在 10/1-7 假日区间内（周四），高峰时段仍放行
        r = off.classify_dispatch(bj(2026, 10, 1, 10, 0))
        self.assertEqual(r['category'], 'holiday')
        self.assertTrue(r['allow'])

    def test_new_year_holiday(self):
        self.assertTrue(off.is_holiday(date(2026, 1, 2)))
        self.assertFalse(off.is_holiday(date(2026, 1, 4)))  # 1/1-3 之外

    def test_spring_range_boundaries(self):
        self.assertTrue(off.is_holiday(date(2026, 2, 15)))
        self.assertTrue(off.is_holiday(date(2026, 2, 23)))
        self.assertFalse(off.is_holiday(date(2026, 2, 24)))

    def test_dragon_boat_and_mid_autumn(self):
        self.assertTrue(off.is_holiday(date(2026, 6, 19)))
        self.assertTrue(off.is_holiday(date(2026, 9, 25)))
        self.assertTrue(off.is_holiday(date(2026, 9, 27)))
        self.assertFalse(off.is_holiday(date(2026, 9, 24)))


class UnknownYearConservative(unittest.TestCase):
    def test_unknown_year_weekday_peak_blocked(self):
        # 2027-01-04 周一，无 2027 放假表 → 高峰保守关闭
        self.assertFalse(off.year_known(date(2027, 1, 4)))
        r = off.classify_dispatch(bj(2027, 1, 4, 10, 0))
        self.assertEqual(r['category'], 'weekday_peak_unknown_year')
        self.assertFalse(r['allow'])
        self.assertIn('holiday table unavailable', r['reason'])

    def test_unknown_year_offhours_allowed(self):
        r = off.classify_dispatch(bj(2027, 1, 4, 20, 0))
        self.assertEqual(r['category'], 'weekday_offpeak')
        self.assertTrue(r['allow'])

    def test_unknown_year_weekend_allowed(self):
        # 周末与年份无关，照常放行
        r = off.classify_dispatch(bj(2027, 1, 2, 10, 0))  # 2027-01-02 周六
        self.assertEqual(r['category'], 'weekend')
        self.assertTrue(r['allow'])

    def test_unknown_year_never_claimed_holiday(self):
        self.assertFalse(off.is_holiday(date(2027, 10, 1)))  # 未知年度不猜假日


class TimezoneCaliber(unittest.TestCase):
    def test_naive_interpreted_as_utc(self):
        # naive 北京时间输入按 UTC 解释：UTC 01:00 → 北京 09:00 周一高峰
        r = off.classify_dispatch(datetime(2026, 10, 12, 1, 0))
        self.assertEqual(r['category'], 'weekday_peak')

    def test_aware_non_utc_converted(self):
        other = timezone(timedelta(hours=5))
        # 06:00 (+05) == 01:00 UTC == 09:00 北京 → 高峰
        dt = datetime(2026, 10, 12, 6, 0, tzinfo=other)
        self.assertEqual(off.classify_dispatch(dt)['category'], 'weekday_peak')

    def test_date_input_supported(self):
        # 传入纯 date（当天 00:00 北京）→ 2026-10-10 周末
        self.assertEqual(off.classify_dispatch(date(2026, 10, 10))['category'], 'weekend')


class SanityHelpers(unittest.TestCase):
    def test_known_years_include_2026_only(self):
        self.assertIn(2026, off.known_years())

    def test_in_peak_minutes_direct(self):
        self.assertTrue(off.in_peak_minutes(datetime(2026, 10, 12, 9, 30)))
        self.assertFalse(off.in_peak_minutes(datetime(2026, 10, 12, 12, 30)))
        self.assertTrue(off.in_peak_minutes(datetime(2026, 10, 12, 15, 0)))
        self.assertFalse(off.in_peak_minutes(datetime(2026, 10, 12, 18, 0)))


if __name__ == '__main__':
    unittest.main()
