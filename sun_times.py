# -*- coding: utf-8 -*-
"""Восход и закат без обращения к сети: расчёт по координатам и часовому поясу.

Модуль нужен для правила «кататься можно только между восходом и закатом»:
по дате, широте, долготе и смещению часового пояса вычисляются минуты восхода
и заката от местной полуночи. Считается алгоритмом солнечных уравнений NOAA
(Solar Calculator) с уточнением на час самого события.

Точность: для Минска расхождение со справочниками (sunrise-sunset.org,
sunrisesunset.io) — около 1–3 минут. Источники расходятся между собой на столько
же (разные высота над морем, рефракция и определение момента касания диска),
поэтому расчёт намеренно чуть консервативен: восход выходит на пару минут позже,
а закат — раньше реального, так что «кататься в темноте» правило не разрешит.

Пример:

    from datetime import date
    from sun_times import sun_times

    day = sun_times(date(2026, 6, 21), lat=53.9022, lon=27.5619, tz_hours=3)
    print(day.text)          # 04:37–21:45
    print(day.sunrise_text)  # 04:37
    print(day.contains(12 * 60))  # True — полдень попадает в светлое время

Использование из командной строки:

    python sun_times.py                                  # Минск, сегодня
    python sun_times.py --date 2026-06-21                # конкретная дата
    python sun_times.py --days 5 --json                  # 5 суток, JSON
    python sun_times.py --lat 53.9 --lon 27.57 --tz 3    # свои координаты и пояс
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

DEFAULT_LAT = 53.9022     # Минск, широта
DEFAULT_LON = 27.5619     # Минск, долгота
DEFAULT_TZ_HOURS = 3.0    # Минск: UTC+3 круглый год
DEFAULT_DAYS = 1          # сколько суток печатать без --days / --date
ZENITH = 90.833           # зенитный угол восхода/заката (90°50') — с учётом рефракции
MINUTES_PER_DAY = 24 * 60
DATE_FORMAT = "%Y-%m-%d"


def minutes_text(minutes: int) -> str:
    """Минуты от полуночи в формате ЧЧ:ММ (1440 -> 24:00)."""
    minutes = min(MINUTES_PER_DAY, max(0, int(minutes)))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class Daylight:
    """Светлое время суток: восход и закат в минутах от местной полуночи.

    polar: None — обычные сутки, "day" — полярный день, "night" — полярная ночь.
    """

    day: str
    sunrise: int
    sunset: int
    polar: str | None = None

    @property
    def duration(self) -> int:
        """Длительность светлого времени, минуты."""
        return max(0, self.sunset - self.sunrise)

    @property
    def sunrise_text(self) -> str:
        """Время восхода строкой ЧЧ:ММ."""
        return minutes_text(self.sunrise)

    @property
    def sunset_text(self) -> str:
        """Время заката строкой ЧЧ:ММ."""
        return minutes_text(self.sunset)

    @property
    def text(self) -> str:
        """Строка для сообщения: '06:45–19:28' (для полярных суток — словами)."""
        if self.polar == "day":
            return "полярный день"
        if self.polar == "night":
            return "полярная ночь"
        return f"{self.sunrise_text}–{self.sunset_text}"

    def contains(self, minutes: int) -> bool:
        """Попадает ли время (минуты от полуночи) в светлое время суток."""
        return self.polar != "night" and self.sunrise <= minutes < self.sunset

    def clamp(self, start: int, end: int) -> tuple[int, int]:
        """Обрезает интервал времени [start, end) по восходу и закату."""
        return max(start, self.sunrise), min(end, self.sunset)


# ---------------------------------------------------------------------------
# Солнечные уравнения NOAA
# ---------------------------------------------------------------------------

def _solar_parameters(day_of_year: int, hour: float = 12.0) -> tuple[float, float]:
    """Уравнение времени (минуты) и склонение Солнца (радианы) на час суток."""
    gamma = 2.0 * math.pi / 365.0 * (day_of_year - 1 + (hour - 12.0) / 24.0)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    return eqtime, declination


def _cos_hour_angle(declination: float, lat: float) -> float:
    """Косинус часового угла восхода (<= -1 — полярный день, >= 1 — полярная ночь)."""
    return (
        math.cos(math.radians(ZENITH))
        / (math.cos(math.radians(lat)) * math.cos(declination))
        - math.tan(math.radians(lat)) * math.tan(declination)
    )


def _clamp_minutes(value: float) -> int:
    """Округление минут и приведение к суткам 0..1440."""
    return min(MINUTES_PER_DAY, max(0, int(round(value))))


def _event_utc_minutes(
    day_of_year: int,
    lat: float,
    lon: float,
    tz_minutes: float,
    rise: bool,
) -> float:
    """Восход (rise=True) или закат в минутах от UTC-полуночи.

    Уравнение времени и склонение уточняются на час самого события (подход NOAA):
    трёх проходов хватает, чтобы попасть в справочные значения с точностью до минуты.
    """
    hour = 12.0
    value = 0.0
    for _ in range(3):
        eqtime, declination = _solar_parameters(day_of_year, hour)
        hour_angle = math.degrees(math.acos(_cos_hour_angle(declination, lat)))
        value = 720.0 - 4.0 * (lon + (hour_angle if rise else -hour_angle)) - eqtime
        hour = (value + tz_minutes) / 60.0
    return value


def sun_times(
    day: date,
    lat: float = DEFAULT_LAT,
    lon: float = DEFAULT_LON,
    tz_hours: float = DEFAULT_TZ_HOURS,
) -> Daylight:
    """Восход и закат на дату: минуты от местной полуночи (часовой пояс — tz_hours)."""
    day_of_year = day.timetuple().tm_yday
    cos_hour_angle = _cos_hour_angle(_solar_parameters(day_of_year)[1], lat)
    if cos_hour_angle <= -1.0:
        return Daylight(day.isoformat(), 0, MINUTES_PER_DAY, polar="day")
    if cos_hour_angle >= 1.0:
        return Daylight(day.isoformat(), MINUTES_PER_DAY // 2, MINUTES_PER_DAY // 2, polar="night")

    tz_minutes = tz_hours * 60.0
    sunrise = _event_utc_minutes(day_of_year, lat, lon, tz_minutes, rise=True)
    sunset = _event_utc_minutes(day_of_year, lat, lon, tz_minutes, rise=False)
    return Daylight(
        day.isoformat(),
        _clamp_minutes(sunrise + tz_minutes),
        _clamp_minutes(sunset + tz_minutes),
    )


def sun_times_by_day(
    days: Iterable[date],
    lat: float = DEFAULT_LAT,
    lon: float = DEFAULT_LON,
    tz_hours: float = DEFAULT_TZ_HOURS,
) -> dict[str, Daylight]:
    """Восход и закат по нескольким датам: {ISO-дата: Daylight}."""
    return {day.isoformat(): sun_times(day, lat, lon, tz_hours) for day in days}


# ---------------------------------------------------------------------------
# Командная строка
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Описывает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        prog="sun_times.py",
        description=(
            "Восход и закат по координатам и часовому поясу (офлайн, алгоритм NOAA): "
            "нужно для правила «кататься только между восходом и закатом»."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--date", default=None, help="Дата начала ГГГГ-ММ-ДД (по умолчанию сегодня)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Сколько суток печатать")
    parser.add_argument("--lat", type=float, default=DEFAULT_LAT, help="Широта")
    parser.add_argument("--lon", type=float, default=DEFAULT_LON, help="Долгота")
    parser.add_argument("--tz", type=float, default=DEFAULT_TZ_HOURS, help="Смещение часового пояса, часы")
    parser.add_argument("--json", action="store_true", help="Вывести результат JSON-ом")
    return parser


def _configure_stdout() -> None:
    """UTF-8 в stdout/stderr, чтобы русский текст не ломался в консоли Windows."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def first_day(text: str | None = None) -> date:
    """Дата начала расчёта: аргумент --date или сегодня."""
    if not text:
        return datetime.now().date()
    try:
        return datetime.strptime(text, DATE_FORMAT).date()
    except ValueError as exc:
        raise ValueError(f"--date ожидает ГГГГ-ММ-ДД, получено {text!r}") from exc


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI. Возвращает код выхода: 0 — успех, 1 — ошибка."""
    _configure_stdout()
    args = build_parser().parse_args(argv)
    if args.days < 1:
        print("Ошибка: --days должно быть не меньше 1.", file=sys.stderr)
        return 1

    try:
        start = first_day(args.date)
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    daylight = sun_times_by_day(
        (start + timedelta(days=offset) for offset in range(args.days)),
        lat=args.lat,
        lon=args.lon,
        tz_hours=args.tz,
    )

    if args.json:
        print(
            json.dumps(
                {
                    day: {
                        "sunrise": item.sunrise_text,
                        "sunset": item.sunset_text,
                        "daylight_minutes": item.duration,
                        "polar": item.polar,
                    }
                    for day, item in daylight.items()
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"Координаты: {args.lat:g}, {args.lon:g}; часовой пояс UTC{args.tz:+g}")
    for item in daylight.values():
        hours, minutes = divmod(item.duration, 60)
        print(
            f"{item.day}: восход {item.sunrise_text}, закат {item.sunset_text} "
            f"({item.text}; светлое время {hours} ч {minutes:02d} мин)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
