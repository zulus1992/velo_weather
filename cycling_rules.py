# -*- coding: utf-8 -*-
"""Правила катания на велосипеде из weather_rules.xlsx и их применение к прогнозу.

Модуль читает таблицу правил (оценка / температура / ветер, км/ч / вероятность осадков, %)
и сопоставляет её с точками прогноза, которые возвращает get_weather_forecast.py.

Пример:

    from cycling_rules import read_rules, evaluate_days, select_days, format_report
    from get_weather_forecast import fetch_forecast, map_forecast

    points = map_forecast(fetch_forecast("Minsk"))
    results = evaluate_days(points, read_rules())
    print(format_report(select_days(results, "tomorrow")))
"""

from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

RULES_FILE = Path(__file__).with_name("weather_rules.xlsx")
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
DAY_FORMAT = "%Y-%m-%d"
POINT_HOURS = 3  # шаг прогноза OpenWeatherMap, часы
DEFAULT_HOUR_FROM = 7   # вердикт считается по светлому времени суток: с 07:00
DEFAULT_HOUR_TO = 22    # ... по 22:00 (22 не включается, т.е. последняя точка 21:00)


class RulesError(RuntimeError):
    """Ошибка чтения или разбора weather_rules.xlsx."""


@dataclass(frozen=True)
class Rule:
    """Строка таблицы правил: оценка и границы значений."""

    name: str
    temp_op: str
    temp_limit: float
    wind_op: str
    wind_limit: float
    pop_op: str
    pop_limit: float

    def describe(self) -> str:
        """Однострочное описание правила (для справки в сообщении)."""
        return (
            f"{self.name}: t {self.temp_op}{self.temp_limit:g} °C, "
            f"ветер {self.wind_op}{self.wind_limit:g} км/ч, "
            f"осадки {self.pop_op}{self.pop_limit:g} %"
        )


def _cell_text(cell: ET.Element, shared: Sequence[str]) -> str:
    """Текст ячейки xlsx (общая строка, inline-строка или обычное значение)."""
    if cell.get("t") == "s":
        value = cell.find(f"{NS}v")
        if value is not None and value.text is not None:
            return shared[int(value.text)]
        return ""
    if cell.get("t") == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{NS}t"))
    value = cell.find(f"{NS}v")
    return value.text if value is not None and value.text is not None else ""


def _threshold(text: str, default_op: str) -> tuple[str, float]:
    """Разбирает порог: '<4' -> ('<', 4.0), '>18' -> ('>', 18.0), '0' -> (default_op, 0.0)."""
    text = (text or "").strip()
    if text.startswith("<"):
        return "<", float(text.lstrip("<"))
    if text.startswith(">"):
        return ">", float(text.lstrip(">"))
    if not text:
        raise ValueError("пустой порог")
    return default_op, float(text)


def read_rules(path: str | Path = RULES_FILE) -> list[Rule]:
    """Читает правила из weather_rules.xlsx (первый лист) в порядке строк таблицы."""
    path = Path(path)
    if not path.is_file():
        raise RulesError(f"Файл правил не найден: {path}")

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [
                "".join(t.text or "" for t in si.iter(f"{NS}t"))
                for si in root.findall(f"{NS}si")
            ]
        sheet_name = next((name for name in names if name.startswith("xl/worksheets/sheet")), None)
        if sheet_name is None:
            raise RulesError(f"В файле {path} нет листов с данными")
        sheet = ET.fromstring(archive.read(sheet_name))

    rules: list[Rule] = []
    for row in sheet.iter(f"{NS}row"):
        cells: dict[str, str] = {}
        for cell in row.findall(f"{NS}c"):
            column = "".join(ch for ch in (cell.get("r") or "") if ch.isalpha())
            if column:
                cells[column] = _cell_text(cell, shared)
        if not cells.get("A") or not cells.get("B", "").startswith(">"):
            continue
        try:
            temp_op, temp_limit = _threshold(cells["B"], ">")
            wind_op, wind_limit = _threshold(cells.get("C", ""), "<")
            pop_op, pop_limit = _threshold(cells.get("D", ""), "<=")
        except (KeyError, ValueError) as exc:
            raise RulesError(f"Не удалось разобрать строку правил {cells}: {exc}") from exc
        rules.append(
            Rule(
                name=cells["A"].strip(),
                temp_op=temp_op,
                temp_limit=temp_limit,
                wind_op=wind_op,
                wind_limit=wind_limit,
                pop_op=pop_op,
                pop_limit=pop_limit,
            )
        )

    if not rules:
        raise RulesError(f"В файле {path} не найдено строк с правилами (ожидались ячейки A/B/C/D)")
    return rules


# ---------------------------------------------------------------------------
# Сопоставление прогноза с правилами
# ---------------------------------------------------------------------------

def _compare(value: Any, op: str, limit: float) -> bool:
    """Сравнивает значение с порогом по оператору правила."""
    if not isinstance(value, (int, float)):
        return False
    if op == ">":
        return value > limit
    if op == "<":
        return value < limit
    return value <= limit


def matches(point: dict[str, Any], rule: Rule) -> bool:
    """Подходит ли точка прогноза под правило (температура, ветер, вероятность осадков)."""
    pop_percent = (point.get("pop") or 0) * 100
    return (
        _compare(point.get("temp"), rule.temp_op, rule.temp_limit)
        and _compare(point.get("wind_speed_kmh"), rule.wind_op, rule.wind_limit)
        and _compare(pop_percent, rule.pop_op, rule.pop_limit)
    )


def day_key(point: dict[str, Any]) -> str:
    """Локальная дата точки прогноза (ISO-строка)."""
    return (point.get("date_local") or point.get("date") or "")[:10]


def group_by_day(points: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Группирует точки прогноза по локальным датам, сохраняя порядок."""
    days: dict[str, list[dict[str, Any]]] = {}
    for point in points:
        days.setdefault(day_key(point), []).append(point)
    return days


def _time_of(point: dict[str, Any]) -> str:
    """Локальное время точки прогноза в формате ЧЧ:ММ."""
    return (point.get("date_local") or point.get("date") or "")[11:16]


def _extreme(points: Sequence[dict[str, Any]], key: str, mode: str) -> float | None:
    """Минимум или максимум значения по списку точек (None, если данных нет)."""
    values = [p.get(key) for p in points if isinstance(p.get(key), (int, float))]
    if not values:
        return None
    return min(values) if mode == "min" else max(values)


def _hour_of(point: dict[str, Any]) -> int | None:
    """Локальный час точки прогноза (None, если время неизвестно)."""
    text = (point.get("date_local") or point.get("date") or "")[11:13]
    return int(text) if text.isdigit() else None


def filter_hours(
    points: Sequence[dict[str, Any]],
    hour_from: int = DEFAULT_HOUR_FROM,
    hour_to: int = DEFAULT_HOUR_TO,
) -> list[dict[str, Any]]:
    """Оставляет точки прогноза с локальным часом в интервале [hour_from, hour_to)."""
    return [
        point
        for point in points
        if (hour := _hour_of(point)) is not None and hour_from <= hour < hour_to
    ]


def _runs(flags: Sequence[bool]) -> list[tuple[int, int]]:
    """Непрерывные участки True: список пар (индекс первого, индекс последнего)."""
    runs: list[tuple[int, int]] = []
    index = 0
    while index < len(flags):
        if not flags[index]:
            index += 1
            continue
        start = index
        while index < len(flags) and flags[index]:
            index += 1
        runs.append((start, index - 1))
    return runs


def _window_text(points: Sequence[dict[str, Any]], run: tuple[int, int]) -> str:
    """Окно в виде диапазона: '09:00–12:00' (конец — последняя точка + шаг прогноза)."""
    start = _time_of(points[run[0]])
    hour, minute = (int(part) for part in _time_of(points[run[1]]).split(":"))
    return f"{start}–{hour + POINT_HOURS:02d}:{minute:02d}"


@dataclass
class DayResult:
    """Итог по одному дню: сводка, лучшее правило и окна времени."""

    day: str
    points: list[dict[str, Any]] = field(default_factory=list)
    best: Rule | None = None
    best_windows: list[str] = field(default_factory=list)

    @property
    def weekday(self) -> str:
        """Название дня недели."""
        return WEEKDAYS[datetime.strptime(self.day, DAY_FORMAT).weekday()]

    @property
    def date_text(self) -> str:
        """Дата в формате ДД.ММ.ГГГГ."""
        return datetime.strptime(self.day, DAY_FORMAT).strftime("%d.%m.%Y")

    @property
    def verdict(self) -> str:
        """Оценка по правилам: категория подходящей строки или «Невозможно»."""
        return self.best.name if self.best else "Невозможно"

    @property
    def temp_min(self) -> float | None:
        """Минимальная температура за день, °C."""
        return _extreme(self.points, "temp", "min")

    @property
    def temp_max(self) -> float | None:
        """Максимальная температура за день, °C."""
        return _extreme(self.points, "temp", "max")

    @property
    def wind_max(self) -> float | None:
        """Максимальный ветер за день, км/ч."""
        return _extreme(self.points, "wind_speed_kmh", "max")

    @property
    def pop_max(self) -> float | None:
        """Максимальная вероятность осадков за день, %."""
        values = [(p.get("pop") or 0) * 100 for p in self.points]
        return max(values) if values else None


def evaluate_day(
    day: str,
    points: Sequence[dict[str, Any]],
    rules: Sequence[Rule],
    min_points: int = 1,
) -> DayResult:
    """Определяет оценку дня и окна времени.

    Правила проверяются по порядку строк таблицы (от лучшей оценки к худшей),
    поэтому первое подошедшее правило и считается вердиктом дня. Правило
    засчитывается, только если есть непрерывное окно длиной не меньше min_points.
    """
    result = DayResult(day=day, points=list(points))
    for rule in rules:
        runs = _runs([matches(point, rule) for point in points])
        if not runs:
            continue
        longest = max(runs, key=lambda run: run[1] - run[0])
        if longest[1] - longest[0] + 1 >= min_points:
            result.best = rule
            result.best_windows = [_window_text(points, run) for run in runs]
            break
    return result


def evaluate_days(
    points: Sequence[dict[str, Any]],
    rules: Sequence[Rule],
    min_points: int = 1,
    hour_from: int = DEFAULT_HOUR_FROM,
    hour_to: int = DEFAULT_HOUR_TO,
) -> list[DayResult]:
    """Считает вердикты по всем дням прогноза (в порядке дат).

    По умолчанию оценка считается по светлому времени суток (hour_from..hour_to),
    чтобы «можно» не появлялось из-за сухой тихой ночи при дождливом дне.
    """
    results = []
    for day, rows in group_by_day(points).items():
        daylight = filter_hours(rows, hour_from, hour_to) or list(rows)
        results.append(evaluate_day(day, daylight, rules, min_points))
    return results


def select_days(
    results: Sequence[DayResult],
    mode: str = "tomorrow",
    today: Any = None,
) -> list[DayResult]:
    """Отбирает дни из результата: today / tomorrow / weekend / all."""
    if mode == "all":
        return list(results)
    today = today or datetime.now().date()
    if mode == "today":
        wanted = {today.isoformat()}
    elif mode == "tomorrow":
        wanted = {(today + timedelta(days=1)).isoformat()}
    elif mode == "weekend":
        weekend = [
            (today + timedelta(days=offset)).isoformat()
            for offset in range(8)
            if (today + timedelta(days=offset)).weekday() in (5, 6)
        ]
        wanted = set(weekend[:2])
    else:
        raise ValueError(f"Неизвестный режим выбора дня: {mode}")
    return [result for result in results if result.day in wanted]


# ---------------------------------------------------------------------------
# Формирование текста сообщения
# ---------------------------------------------------------------------------

def _temp_text(value: float) -> str:
    """Температура со знаком: +10.3 / -2.5."""
    return f"{value:+.1f}"


def _value_text(value: float | None) -> str:
    """Число для сообщения («—», если данных нет)."""
    return "—" if value is None else f"{value:g}"


def format_day(result: DayResult) -> str:
    """Формирует блок сообщения по одному дню."""
    if result.temp_min is not None and result.temp_max is not None:
        temps = f"{_temp_text(result.temp_min)}…{_temp_text(result.temp_max)}"
    else:
        temps = "—"
    lines = [
        f"Вердикт: {result.verdict}",
        f"• температура: {temps} °C",
        f"• ветер: до {_value_text(result.wind_max)} км/ч",
        f"• осадки: до {_value_text(result.pop_max)} %",
    ]
    if result.best:
        label = "окна" if len(result.best_windows) > 1 else "окно"
        lines.append(f"• {label}: {', '.join(result.best_windows)}")
    else:
        lines.append("• подходящих окон по правилам нет")
    return f"{result.weekday} {result.date_text}\n" + "\n".join(lines)


def format_report(
    results: Sequence[DayResult],
    title: str = "Прогноз для велосипеда",
    rules: Sequence[Rule] = (),
) -> str:
    """Готовый текст сообщения с вердиктами по дням (в том числе для Telegram)."""
    blocks = [title]
    for result in results:
        blocks.extend(("", format_day(result)))
    if rules:
        blocks.extend(("", "Правила:"))
        blocks.extend(f"• {rule.describe()}" for rule in rules)
    return "\n".join(blocks).strip()
