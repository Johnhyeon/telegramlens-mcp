"""Market session calendar helpers for StockLens."""
from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class SessionHours:
    pre_open: time | None
    regular_open: time
    regular_close: time
    after_close: time | None


KRX_HOURS = SessionHours(
    pre_open=time(8, 30),
    regular_open=time(9, 0),
    regular_close=time(15, 30),
    after_close=None,
)
US_HOURS = SessionHours(
    pre_open=time(4, 0),
    regular_open=time(9, 30),
    regular_close=time(16, 0),
    after_close=time(20, 0),
)


KRX_YEAR_SPECIFIC_HOLIDAYS = {
    2025: {
        date(2025, 1, 28): "Lunar New Year",
        date(2025, 1, 29): "Lunar New Year",
        date(2025, 1, 30): "Lunar New Year",
        date(2025, 3, 3): "Substitute holiday",
        date(2025, 5, 6): "Substitute holiday",
        date(2025, 6, 3): "Presidential Election Day",
        date(2025, 10, 6): "Chuseok",
        date(2025, 10, 7): "Chuseok",
        date(2025, 10, 8): "Substitute holiday",
    },
    2026: {
        date(2026, 2, 16): "Lunar New Year",
        date(2026, 2, 17): "Lunar New Year",
        date(2026, 2, 18): "Lunar New Year",
        date(2026, 3, 2): "Substitute holiday for Independence Movement Day",
        date(2026, 5, 25): "Buddha's Birthday",
        date(2026, 6, 3): "Election Day",
        date(2026, 8, 17): "Substitute holiday for Liberation Day",
        date(2026, 9, 24): "Chuseok",
        date(2026, 9, 25): "Chuseok",
        date(2026, 9, 28): "Substitute holiday for Chuseok",
    },
    2027: {
        date(2027, 2, 8): "Lunar New Year",
        date(2027, 2, 9): "Lunar New Year",
        date(2027, 2, 10): "Lunar New Year",
        date(2027, 5, 13): "Buddha's Birthday",
        date(2027, 8, 16): "Substitute holiday for Liberation Day",
        date(2027, 9, 14): "Chuseok",
        date(2027, 9, 15): "Chuseok",
        date(2027, 9, 16): "Chuseok",
        date(2027, 10, 4): "Substitute holiday for National Foundation Day",
    },
}


def get_market_clock(now: datetime | None = None) -> dict:
    """Return KRX and US equity market session state for a point in time."""
    base = _coerce_datetime(now)
    now_kst = base.astimezone(KST)
    now_et = base.astimezone(ET)
    krx = _build_market_state(
        market="KRX",
        timezone="Asia/Seoul",
        now_local=now_kst,
        hours=KRX_HOURS,
        holiday_name_func=_krx_holiday_name,
    )
    us = _build_market_state(
        market="NYSE/NASDAQ",
        timezone="America/New_York",
        now_local=now_et,
        hours=_us_hours(now_et.date()),
        holiday_name_func=_us_holiday_name,
    )
    return {
        "source": "stocklens.market_clock",
        "now_kst": now_kst.isoformat(timespec="seconds"),
        "now_et": now_et.isoformat(timespec="seconds"),
        "krx": krx,
        "us": us,
        "warnings": [
            "Holiday calendars include regular exchange holidays and known 2025-2027 KRX date-specific closures.",
            "Unexpected exchange closures are not knowable until the exchange announces them.",
        ],
    }


def format_market_clock(clock: dict) -> str:
    krx = clock["krx"]
    us = clock["us"]
    lines = [
        "시장 캘린더",
        f"- 기준: {clock['now_kst']} KST / {clock['now_et']} ET",
        f"- 한국장: {_status_label(krx['status'])} ({krx['reason']})",
        f"  최근 거래일: {krx['last_trading_day']} / 다음 개장일: {krx['next_trading_day']}",
        f"- 미국장: {_status_label(us['status'])} ({us['reason']})",
        f"  최근 거래일: {us['last_trading_day']} / 다음 개장일: {us['next_trading_day']}",
        "",
        "MARKET_CLOCK_JSON_START",
        json.dumps(clock, ensure_ascii=False, sort_keys=True),
        "MARKET_CLOCK_JSON_END",
    ]
    return "\n".join(lines)


def _build_market_state(
    *,
    market: str,
    timezone: str,
    now_local: datetime,
    hours: SessionHours,
    holiday_name_func,
) -> dict:
    current_date = now_local.date()
    holiday_name = holiday_name_func(current_date)
    is_weekend = current_date.weekday() >= 5
    is_trading_day = not is_weekend and holiday_name is None
    status, reason = _session_status(now_local.time(), hours, is_weekend, holiday_name)
    if is_trading_day and now_local.time() >= hours.regular_open:
        last_trading_day = current_date
    else:
        last_trading_day = _shift_trading_day(current_date, -1, holiday_name_func)
    if is_trading_day and now_local.time() < hours.regular_open:
        next_trading_day = current_date
    else:
        next_trading_day = _shift_trading_day(current_date, 1, holiday_name_func)
    return {
        "market": market,
        "timezone": timezone,
        "status": status,
        "is_open": status in {"regular"},
        "is_trading_day": is_trading_day,
        "is_weekend": is_weekend,
        "is_holiday": holiday_name is not None,
        "reason": reason,
        "last_trading_day": last_trading_day.isoformat(),
        "next_trading_day": next_trading_day.isoformat(),
        "regular_open": hours.regular_open.strftime("%H:%M"),
        "regular_close": hours.regular_close.strftime("%H:%M"),
    }


def _session_status(local_time: time, hours: SessionHours, is_weekend: bool, holiday_name: str | None) -> tuple[str, str]:
    if is_weekend:
        return "closed_weekend", "Weekend"
    if holiday_name:
        return "closed_holiday", holiday_name
    if hours.pre_open and hours.pre_open <= local_time < hours.regular_open:
        return "pre_market", "Before regular session"
    if local_time < hours.regular_open:
        return "closed_before_open", "Before pre-market or opening session"
    if hours.regular_open <= local_time < hours.regular_close:
        return "regular", "Regular session"
    if hours.after_close and hours.regular_close <= local_time < hours.after_close:
        return "after_hours", "After-hours session"
    return "closed_after_hours", "Regular session ended"


def _shift_trading_day(start: date, direction: int, holiday_name_func) -> date:
    cursor = start + timedelta(days=direction)
    while cursor.weekday() >= 5 or holiday_name_func(cursor) is not None:
        cursor += timedelta(days=direction)
    return cursor


def _coerce_datetime(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(KST)
    if now.tzinfo is None:
        return now.replace(tzinfo=KST)
    return now


def _krx_holiday_name(day: date) -> str | None:
    fixed = {
        (1, 1): "New Year's Day",
        (3, 1): "Independence Movement Day",
        (5, 1): "Labor Day",
        (5, 5): "Children's Day",
        (6, 6): "Memorial Day",
        (8, 15): "Liberation Day",
        (10, 3): "National Foundation Day",
        (10, 9): "Hangul Day",
        (12, 25): "Christmas Day",
        (12, 31): "Year-end market closure",
    }
    if (day.month, day.day) in fixed:
        return fixed[(day.month, day.day)]
    return KRX_YEAR_SPECIFIC_HOLIDAYS.get(day.year, {}).get(day)


def _us_holiday_name(day: date) -> str | None:
    holidays = {
        _observed_fixed(day.year, 1, 1): "New Year's Day",
        _nth_weekday(day.year, 1, calendar.MONDAY, 3): "Martin Luther King Jr. Day",
        _nth_weekday(day.year, 2, calendar.MONDAY, 3): "Washington's Birthday",
        _good_friday(day.year): "Good Friday",
        _last_weekday(day.year, 5, calendar.MONDAY): "Memorial Day",
        _observed_fixed(day.year, 6, 19): "Juneteenth National Independence Day",
        _observed_fixed(day.year, 7, 4): "Independence Day",
        _nth_weekday(day.year, 9, calendar.MONDAY, 1): "Labor Day",
        _nth_weekday(day.year, 11, calendar.THURSDAY, 4): "Thanksgiving Day",
        _observed_fixed(day.year, 12, 25): "Christmas Day",
    }
    return holidays.get(day)


def _us_hours(day: date) -> SessionHours:
    if day in _us_early_close_days(day.year):
        return SessionHours(
            pre_open=US_HOURS.pre_open,
            regular_open=US_HOURS.regular_open,
            regular_close=time(13, 0),
            after_close=US_HOURS.after_close,
        )
    return US_HOURS


def _us_early_close_days(year: int) -> set[date]:
    days = {
        _nth_weekday(year, 11, calendar.THURSDAY, 4) + timedelta(days=1),
        date(year, 12, 24),
    }
    july_4 = date(year, 7, 4)
    if july_4.weekday() == calendar.SATURDAY:
        days.add(july_4 - timedelta(days=2))
    elif july_4.weekday() == calendar.SUNDAY:
        days.add(july_4 - timedelta(days=2))
    elif july_4.weekday() not in {calendar.MONDAY, calendar.FRIDAY}:
        days.add(july_4 - timedelta(days=1))
    return {day for day in days if day.weekday() < 5 and _us_holiday_name(day) is None}


def _observed_fixed(year: int, month: int, day: int) -> date:
    actual = date(year, month, day)
    if actual.weekday() == calendar.SATURDAY:
        return actual - timedelta(days=1)
    if actual.weekday() == calendar.SUNDAY:
        return actual + timedelta(days=1)
    return actual


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _good_friday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day) - timedelta(days=2)


def _status_label(status: str) -> str:
    return {
        "closed_weekend": "주말 휴장",
        "closed_holiday": "휴장",
        "closed_before_open": "개장 전",
        "pre_market": "장전",
        "regular": "정규장",
        "after_hours": "시간외",
        "closed_after_hours": "장마감",
    }.get(status, status)
