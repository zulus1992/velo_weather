# -*- coding: utf-8 -*-
"""Расписание рассылки прогноза в Telegram: когда и какой прогноз публиковать.

Расписание живёт в weather_schedule.json: слоты с местным временем (timezone и
utc_offset_hours), днями недели, режимом прогноза (--day для send_telegram.py) и
служебными флагами. GitHub Actions не умеет читать расписание из файла, поэтому в
.github/workflows/weather.yml те же моменты указаны cron-строками; строки считает
--cron-lines, а сверяет их с файлом --check. При срабатывании расписания workflow
вызывает --cron со значением github.event.schedule, и скрипт отвечает, что публиковать.

Использование как модуля:

    from schedule_slots import cron_for, load_schedule, slot_by_cron

    schedule = load_schedule()                                  # weather_schedule.json
    cron_for(schedule.slots[0], schedule.utc_offset_hours)      # '30 3 * * *'
    slot_by_cron(schedule, "30 3 * * *")                        # Slot(name='утро', ...)

Использование из командной строки:

    python schedule_slots.py --show                    # таблица «когда что отправляется»
    python schedule_slots.py --cron-lines              # cron-строки для workflow
    python schedule_slots.py --cron "30 3 * * *"       # что публиковать в этот слот
    python schedule_slots.py --check                   # сверка weather_schedule.json и workflow
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from sun_times import MINUTES_PER_DAY

SCHEDULE_FILE = Path(__file__).with_name("weather_schedule.json")
WORKFLOW_FILE = Path(__file__).with_name(".github") / "workflows" / "weather.yml"
DAY_CHOICES = ("today", "tomorrow", "weekend", "all")   # значения --day у send_telegram.py
DEFAULT_DAY = "tomorrow"                                # режим по умолчанию (как раньше)
WEEKDAY_ORDER = (1, 2, 3, 4, 5, 6, 7)                   # 1 — понедельник, 7 — воскресенье
WEEKDAY_LABELS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
ALL_DAYS_TEXT = "пн-вс"


class ScheduleError(RuntimeError):
    """Ошибка чтения расписания weather_schedule.json."""


@dataclass(frozen=True)
class Slot:
    """Один слот рассылки: когда публиковать и какой прогноз."""

    name: str
    time_local: str            # 'ЧЧ:ММ' в часовом поясе расписания
    weekdays: tuple[int, ...]  # 1=Пн … 7=Вс
    day: str                   # --day для send_telegram.py: today/tomorrow/weekend/all
    silent: bool = False       # --silent: без звука
    show_rules: bool = False   # --show-rules: добавить таблицу правил в сообщение
    enabled: bool = True       # false — слот оставлен в файле, но не публикуется
    about: str = ""            # пояснение для логов и README

    @property
    def hour_minute(self) -> tuple[int, int]:
        """Час и минуты публикации по местному времени."""
        return _parse_time(self.time_local)

    def args_text(self) -> str:
        """Аргументы для send_telegram.py: '--day today --show-rules --silent'."""
        args = [f"--day {self.day}"]
        if self.show_rules:
            args.append("--show-rules")
        if self.silent:
            args.append("--silent")
        return " ".join(args)


@dataclass(frozen=True)
class Schedule:
    """Расписание целиком: часовой пояс, сдвиг от UTC и слоты рассылки."""

    timezone: str
    utc_offset_hours: float
    slots: tuple[Slot, ...]
    source: Path


# ---------------------------------------------------------------------------
# Чтение расписания
# ---------------------------------------------------------------------------

def _parse_time(value: Any) -> tuple[int, int]:
    """Разбирает местное время слота: '6:30' или '06:30' -> (6, 30)."""
    text = str(value or "").strip()
    match = TIME_RE.fullmatch(text)
    if match is None:
        raise ScheduleError(f"time_local должен быть в виде ЧЧ:ММ (получено {text!r}).")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ScheduleError(f"time_local вне диапазона 00:00…23:59 (получено {text!r}).")
    return hour, minute


def _parse_weekdays(value: Any) -> tuple[int, ...]:
    """Дни недели слота (1=Пн … 7=Вс); пустое значение или 'all' — каждый день."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "*", "all")):
        return WEEKDAY_ORDER
    if isinstance(value, int):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ScheduleError(f"weekdays должен быть списком дней 1…7 (получено {value!r}).")
    days: list[int] = []
    for item in value:
        try:
            day = int(item)
        except (TypeError, ValueError) as exc:
            raise ScheduleError(f"День недели должен быть числом 1…7 (получено {item!r}).") from exc
        if not 1 <= day <= 7:
            raise ScheduleError(f"День недели должен быть в диапазоне 1…7 (получено {day}).")
        days.append(day)
    if not days:
        raise ScheduleError("weekdays не может быть пустым списком: уберите ключ или укажите дни.")
    return tuple(sorted(set(days)))


def _slot_from(raw: Any, index: int, path: Path) -> Slot:
    """Собирает слот из элемента списка slots в weather_schedule.json."""
    if not isinstance(raw, dict):
        raise ScheduleError(f"{path.name}: слот №{index} должен быть объектом JSON.")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ScheduleError(f"{path.name}: у слота №{index} не задано имя \"name\".")
    hour, minute = _parse_time(raw.get("time_local"))
    day = str(raw.get("day") or DEFAULT_DAY).strip().lower()
    if day not in DAY_CHOICES:
        raise ScheduleError(
            f"{path.name}: слот «{name}»: day должен быть одним из {', '.join(DAY_CHOICES)} "
            f"(получено {day!r})."
        )
    return Slot(
        name=name,
        time_local=f"{hour:02d}:{minute:02d}",
        weekdays=_parse_weekdays(raw.get("weekdays")),
        day=day,
        silent=bool(raw.get("silent", False)),
        show_rules=bool(raw.get("show_rules", False)),
        enabled=bool(raw.get("enabled", True)),
        about=str(raw.get("about") or "").strip(),
    )


def load_schedule(path: str | Path = SCHEDULE_FILE) -> Schedule:
    """Читает weather_schedule.json: часовой пояс, сдвиг от UTC и слоты рассылки."""
    path = Path(path)
    if not path.is_file():
        raise ScheduleError(f"Файл расписания не найден: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScheduleError(f"Не удалось прочитать {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ScheduleError(f"{path.name}: ожидается объект JSON с ключами timezone, utc_offset_hours и slots.")

    offset = data.get("utc_offset_hours", 0)
    try:
        offset_hours = float(offset)
    except (TypeError, ValueError) as exc:
        raise ScheduleError(f"{path.name}: utc_offset_hours должен быть числом (получено {offset!r}).") from exc
    if not -12 <= offset_hours <= 14:
        raise ScheduleError(f"{path.name}: utc_offset_hours вне диапазона -12…+14 (получено {offset_hours}).")

    raw_slots = data.get("slots")
    if not isinstance(raw_slots, list) or not raw_slots:
        raise ScheduleError(f"{path.name}: нужен непустой список слотов \"slots\".")
    slots = tuple(_slot_from(raw, index, path) for index, raw in enumerate(raw_slots, start=1))
    names = [slot.name for slot in slots]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ScheduleError(f"{path.name}: имена слотов повторяются: {', '.join(duplicates)}.")

    return Schedule(
        timezone=str(data.get("timezone") or "UTC"),
        utc_offset_hours=offset_hours,
        slots=slots,
        source=path,
    )


# ---------------------------------------------------------------------------
# Cron и текстовые подписи
# ---------------------------------------------------------------------------

def _ranges(days: Sequence[int]) -> list[tuple[int, int]]:
    """Группирует дни недели в непрерывные диапазоны: (1,2,3,4,6,7) -> [(1,4),(6,7)]."""
    ranges: list[tuple[int, int]] = []
    for day in sorted(set(days)):
        if ranges and day == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ranges


def _shift_weekdays(days: Sequence[int], shift: int) -> tuple[int, ...]:
    """Сдвигает дни недели на shift шагов (нужно, когда слот переходит через полночь UTC)."""
    if not shift:
        return tuple(days)
    return tuple(sorted({(day - 1 + shift) % 7 + 1 for day in days}))


def _cron_weekdays(days: Sequence[int]) -> str:
    """Поле дней недели для cron: все дни -> '*', иначе '1-4,6' (воскресенье — 0, как принято в cron)."""
    if len(set(days)) == len(WEEKDAY_ORDER):
        return "*"
    cron_days = sorted({0 if day == 7 else day for day in days})
    return ",".join(f"{start}-{end}" if start != end else str(start) for start, end in _ranges(cron_days))


def cron_for(slot: Slot, utc_offset_hours: float = 0.0) -> str:
    """Cron-строка слота в UTC: '30 3 * * *' — 06:30 по Минску (UTC+3)."""
    hour, minute = slot.hour_minute
    total = hour * 60 + minute - int(round(utc_offset_hours * 60))
    day_shift = -1 if total < 0 else (1 if total >= MINUTES_PER_DAY else 0)
    utc_hour, utc_minute = divmod(total % MINUTES_PER_DAY, 60)
    weekdays = _shift_weekdays(slot.weekdays, day_shift)
    return f"{utc_minute} {utc_hour} * * {_cron_weekdays(weekdays)}"


def _offset_text(utc_offset_hours: float) -> str:
    """Сдвиг от UTC в виде 'UTC+3' или 'UTC+5:30'."""
    total = int(round(utc_offset_hours * 60))
    hours, minutes = divmod(abs(total), 60)
    return f"UTC{'+' if total >= 0 else '-'}{hours}" + (f":{minutes:02d}" if minutes else "")


def _weekdays_text(days: Sequence[int]) -> str:
    """Дни недели словами: все дни -> 'пн-вс', иначе 'пн-чт,сб-вс'."""
    if len(set(days)) == len(WEEKDAY_ORDER):
        return ALL_DAYS_TEXT
    parts = []
    for start, end in _ranges(days):
        if start == end:
            parts.append(WEEKDAY_LABELS[start - 1])
        else:
            parts.append(f"{WEEKDAY_LABELS[start - 1]}-{WEEKDAY_LABELS[end - 1]}")
    return ",".join(parts)


def slot_by_cron(schedule: Schedule, cron: str) -> Slot | None:
    """Ищет слот по cron-строке сработавшего запуска (github.event.schedule); None — не найден."""
    wanted = " ".join(str(cron or "").split())
    if not wanted:
        return None
    for slot in schedule.slots:
        if cron_for(slot, schedule.utc_offset_hours) == wanted:
            return slot
    return None


def format_table(schedule: Schedule) -> str:
    """Таблица «когда и что публикуется» — для логов и документации."""
    lines = [
        f"Расписание рассылки ({schedule.source.name}, {schedule.timezone}, "
        f"{_offset_text(schedule.utc_offset_hours)}):"
    ]
    if not schedule.slots:
        return lines[0]
    name_width = max(len(slot.name) for slot in schedule.slots)
    days_width = max(len(_weekdays_text(slot.weekdays)) for slot in schedule.slots)
    args_width = max(len(slot.args_text()) for slot in schedule.slots)
    for slot in schedule.slots:
        lines.append(
            (
                f"  {slot.name.ljust(name_width)}  {slot.time_local}  "
                f"{_weekdays_text(slot.weekdays).ljust(days_width)}  "
                f"{slot.args_text().ljust(args_width)}  {slot.about}"
                f"{'' if slot.enabled else '  (выключен)'}"
            ).rstrip()
        )
    lines.append(
        "  cron (UTC): "
        + " | ".join(cron_for(slot, schedule.utc_offset_hours) for slot in schedule.slots)
    )
    return "\n".join(lines)


def cron_lines(schedule: Schedule) -> list[str]:
    """Cron-строки для блока on.schedule в workflow — их печатает --cron-lines."""
    return [
        f"    - cron: \"{cron_for(slot, schedule.utc_offset_hours)}\""
        f"  # {slot.time_local} {schedule.timezone} ({_weekdays_text(slot.weekdays)})"
        f" -> {slot.args_text()}"
        f"{'' if slot.enabled else '  (выключен в ' + schedule.source.name + ')'}"
        for slot in schedule.slots
    ]


# ---------------------------------------------------------------------------
# Сверка с workflow и вывод для GitHub Actions
# ---------------------------------------------------------------------------

def workflow_crons(path: str | Path = WORKFLOW_FILE) -> list[str]:
    """Cron-строки из .github/workflows/weather.yml (комментарии отбрасываются)."""
    path = Path(path)
    if not path.is_file():
        raise ScheduleError(f"Файл workflow не найден: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScheduleError(f"Не удалось прочитать {path}: {exc}") from exc

    crons: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-") or "cron:" not in stripped:
            continue
        value = stripped.split("cron:", 1)[1].split("#", 1)[0].strip().strip("'\"")
        if value:
            crons.append(" ".join(value.split()))
    return crons


def check_workflow(schedule: Schedule, path: str | Path = WORKFLOW_FILE) -> list[str]:
    """Сверяет cron в workflow с расписанием: список расхождений (пусто — всё сходится)."""
    path = Path(path)
    expected = {
        cron_for(slot, schedule.utc_offset_hours): slot
        for slot in schedule.slots
        if slot.enabled
    }
    found = workflow_crons(path)
    problems = [
        f"в {path.name} нет cron \"{cron}\" для слота «{slot.name}» "
        f"({slot.time_local} {schedule.timezone}) — добавьте строки: python schedule_slots.py --cron-lines"
        for cron, slot in expected.items()
        if cron not in found
    ]
    problems += [
        f"в {path.name} лишний cron \"{cron}\": такого слота нет в {schedule.source.name}"
        for cron in found
        if cron not in expected
    ]
    return problems


def append_github_output(path: str | Path, outputs: Mapping[str, str]) -> None:
    """Дописывает параметры шага в файл GITHUB_OUTPUT (формат «ключ=значение»)."""
    try:
        with open(path, "a", encoding="utf-8") as file:
            for key, value in outputs.items():
                file.write(f"{key}={value}\n")
    except OSError as exc:
        raise ScheduleError(f"Не удалось записать {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Командная строка
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Описывает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        prog="schedule_slots.py",
        description=(
            "Расписание рассылки погоды в Telegram: когда и какой прогноз публикуется "
            "(weather_schedule.json), cron-строки для GitHub Actions и сверка с workflow."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--file", default=str(SCHEDULE_FILE), help="Файл расписания (JSON)")
    parser.add_argument(
        "--workflow",
        default=str(WORKFLOW_FILE),
        help="Файл workflow для сверки в режиме --check",
    )
    parser.add_argument("--show", action="store_true", help="Показать таблицу расписания")
    parser.add_argument(
        "--cron-lines",
        dest="cron_lines",
        action="store_true",
        help="Напечатать cron-строки для блока on.schedule в workflow",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Сверить cron в workflow с расписанием (код выхода 1 при расхождении)",
    )
    parser.add_argument(
        "--cron",
        nargs="?",
        const="",
        default=None,
        help=(
            "Cron сработавшего запуска: в Actions — ${{ github.event.schedule }} "
            "(без значения — ручной запуск)"
        ),
    )
    parser.add_argument(
        "--manual-day",
        dest="manual_day",
        default=None,
        help=f"Период прогноза для ручного запуска: {', '.join(DAY_CHOICES)}",
    )
    parser.add_argument(
        "--github-output",
        dest="github_output",
        default=None,
        help="Файл GITHUB_OUTPUT для передачи параметров шага (по умолчанию — переменная окружения)",
    )
    return parser


def _configure_stdout() -> None:
    """UTF-8 в stdout/stderr, чтобы русский текст не ломался в консоли и в логе."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа CLI. Код выхода: 0 — успех, 1 — ошибка расписания."""
    _configure_stdout()
    args = build_parser().parse_args(argv)

    try:
        schedule = load_schedule(args.file)
    except ScheduleError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    if args.show or args.cron_lines or args.check or args.cron is not None:
        print(format_table(schedule))
    if args.cron_lines:
        print(f"\nСтроки для on.schedule в {Path(args.workflow).name}:")
        print("\n".join(cron_lines(schedule)))
    if args.check:
        try:
            problems = check_workflow(schedule, args.workflow)
        except ScheduleError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        for problem in problems:
            print(f"Ошибка: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(
            f"Расписание сходится: cron в {Path(args.workflow).name} "
            f"совпадает с {schedule.source.name}."
        )

    if args.cron is None:
        return 0

    cron = " ".join(str(args.cron).split())
    manual_day = str(args.manual_day or "").strip().lower()
    if manual_day and manual_day not in DAY_CHOICES:
        print(
            f"Ошибка: --manual-day должен быть одним из {', '.join(DAY_CHOICES)} "
            f"(получено {manual_day!r}).",
            file=sys.stderr,
        )
        return 1

    slot = slot_by_cron(schedule, cron)
    if slot is None:
        day = manual_day or DEFAULT_DAY
        args_text = f"--day {day}"
        if cron:
            print(
                f"Предупреждение: cron \"{cron}\" не описан в {schedule.source.name}.",
                file=sys.stderr,
            )
            print(f"Публикую как при ручном запуске: python send_telegram.py {args_text}")
        else:
            print(f"Ручной запуск (cron пуст): python send_telegram.py {args_text}")
        outputs = {"slot": "—", "day": day, "silent": "false", "skip": "false", "args": args_text}
    elif not slot.enabled:
        print(f"Слот «{slot.name}» выключен в {schedule.source.name}: публикация пропускается.")
        outputs = {"slot": slot.name, "day": slot.day, "silent": "false", "skip": "true", "args": ""}
    else:
        print(
            f"Сработал слот «{slot.name}» ({slot.time_local} {schedule.timezone}): "
            f"python send_telegram.py {slot.args_text()}"
        )
        outputs = {
            "slot": slot.name,
            "day": slot.day,
            "silent": "true" if slot.silent else "false",
            "skip": "false",
            "args": slot.args_text(),
        }

    output_path = args.github_output or os.getenv("GITHUB_OUTPUT") or ""
    if output_path:
        try:
            append_github_output(output_path, outputs)
        except ScheduleError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


