# -*- coding: utf-8 -*-
"""Инструмент получения прогноза погоды для ассистента-райдера.

Источник данных: OpenWeatherMap Forecast API (5 суток, шаг 3 часа).
Каждая точка прогноза приводится к плоской структуре:

    {
        "date": "2026-09-21 12:00:00",        # dt_txt — время точки прогноза (UTC)
        "date_local": "2026-09-21 15:00:00",  # то же время в часовом поясе города
        "temp": 14.04,                        # main.temp, °C (units="metric")
        "wind_speed": 3.26,                   # wind.speed, м/с
        "wind_speed_kmh": 11.74,              # wind.speed * 3.6 — правила в км/ч
        "condition": "Clouds",                # weather[0].main: Clouds / Rain / Snow / Clear / ...
        "pop": 0.0                            # вероятность осадков, 0..1 (0.95 = 95 %)
    }

Использование как инструмента (импорт):

    from get_weather_forecast import get_weather_forecast, get_tomorrow_forecast

    forecast = get_weather_forecast("Minsk")   # все 40 точек (5 суток)
    tomorrow = get_tomorrow_forecast("Minsk")  # только завтрашние точки (8 точек)

Использование из командной строки:

    python get_weather_forecast.py                       # Минск, все точки, JSON в stdout
    python get_weather_forecast.py --tomorrow            # только точки на завтра
    python get_weather_forecast.py --daily               # агрегированный прогноз по суткам
    python get_weather_forecast.py --city Minsk --out forecast.json
    python get_weather_forecast.py --save-raw response.json              # сохранить сырой ответ API
    python get_weather_forecast.py --from-file response.json --tomorrow  # разбор без обращения к сети

Ключ API берётся из переменной окружения WEATHER_API_KEY (в GitHub Actions — из секрета
с тем же именем), поддерживается и старое имя OPENWEATHER_API_KEY, а также аргумент
--api-key. В коде ключ не хранится.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

BASE_URL = "https://api.openweathermap.org/data/2.5/forecast"

DEFAULT_CITY = "Minsk"       # город по умолчанию (запрос параметром q)
DEFAULT_UNITS = "metric"     # °C и м/с
DEFAULT_LANG = "ru"
DEFAULT_TIMEOUT = 15.0

# Ключ OpenWeatherMap берётся из переменной окружения WEATHER_API_KEY
# (в GitHub Actions — из секрета WEATHER_API_KEY, см. .github/workflows/weather.yml).
# В коде ключ не хранится: если переменная не задана, скрипт скажет об этом понятной ошибкой.
DEFAULT_API_KEY = os.getenv("WEATHER_API_KEY", "")

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class WeatherApiError(RuntimeError):
    """Ошибка запроса к OpenWeatherMap или разбора его ответа."""


def _clean_key(value: Any) -> str:
    """Убирает пробелы и кавычки — частая ошибка при вставке ключа или секрета."""
    return str(value or "").strip().strip("'\"").strip()


def get_api_key(api_key: str | None = None) -> str:
    """Возвращает ключ API: аргумент -> WEATHER_API_KEY -> OPENWEATHER_API_KEY -> DEFAULT_API_KEY."""
    key = (
        _clean_key(api_key)
        or _clean_key(os.getenv("WEATHER_API_KEY"))
        or _clean_key(os.getenv("OPENWEATHER_API_KEY"))
        or _clean_key(DEFAULT_API_KEY)
    )
    if not key or key == "YOUR_API_KEY":
        raise WeatherApiError(
            "Не задан ключ OpenWeatherMap: укажите --api-key или задайте переменную "
            "окружения WEATHER_API_KEY (в GitHub Actions — секрет WEATHER_API_KEY)."
        )
    return key


def build_params(
    city: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    api_key: str | None = None,
    units: str = DEFAULT_UNITS,
    lang: str = DEFAULT_LANG,
) -> dict[str, Any]:
    """Собирает query-параметры: по координатам (lat/lon), иначе по названию города (q)."""
    params: dict[str, Any] = {"appid": get_api_key(api_key), "units": units, "lang": lang}
    if lat is not None and lon is not None:
        params["lat"] = lat
        params["lon"] = lon
    else:
        params["q"] = city or DEFAULT_CITY
    return params


def _validate_payload(data: Any) -> dict[str, Any]:
    """Проверяет структуру ответа API и возвращает его как словарь."""
    if not isinstance(data, dict):
        raise WeatherApiError("Ответ OpenWeatherMap не является объектом JSON.")
    if str(data.get("cod")) != "200":
        raise WeatherApiError(
            f"OpenWeatherMap вернул ошибку: {data.get('message') or data.get('cod')}"
        )
    if not data.get("list"):
        raise WeatherApiError("В ответе OpenWeatherMap нет массива прогноза (list).")
    return data


def fetch_forecast(
    city: str | None = None,
    *,
    lat: float | None = None,
    lon: float | None = None,
    api_key: str | None = None,
    units: str = DEFAULT_UNITS,
    lang: str = DEFAULT_LANG,
    timeout: float = DEFAULT_TIMEOUT,
    session: Any = None,
) -> dict[str, Any]:
    """Запрашивает прогноз у OpenWeatherMap и возвращает ответ API «как есть»."""
    params = build_params(city, lat, lon, api_key, units, lang)
    http = session or requests
    try:
        response = http.get(BASE_URL, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise WeatherApiError(f"Не удалось обратиться к OpenWeatherMap: {exc}") from exc

    if response.status_code == 401:
        raise WeatherApiError("OpenWeatherMap отклонил ключ API (HTTP 401): проверьте ключ.")
    if response.status_code == 404:
        raise WeatherApiError(
            f"Город не найден (HTTP 404): {params.get('q') or params.get('lat')}."
        )
    if response.status_code == 429:
        raise WeatherApiError("Превышен лимит запросов к OpenWeatherMap (HTTP 429).")
    if response.status_code != 200:
        raise WeatherApiError(
            f"OpenWeatherMap вернул HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        return _validate_payload(response.json())
    except ValueError as exc:
        raise WeatherApiError("Ответ OpenWeatherMap не является корректным JSON.") from exc


def load_forecast_file(path: str) -> dict[str, Any]:
    """Читает сохранённый «сырой» ответ API из файла (офлайн-разбор без обращения к сети).

    Ожидает ответ OpenWeatherMap целиком (с ключами cod/list) — например, файл,
    сохранённый командой `--save-raw response.json`.
    """
    with open(path, encoding="utf-8") as file:
        try:
            data = json.load(file)
        except json.JSONDecodeError as exc:
            raise WeatherApiError(f"Файл {path} не является корректным JSON: {exc}") from exc
    if isinstance(data, list):
        raise WeatherApiError(
            f"Файл {path} содержит уже преобразованный список точек прогноза, "
            "а не сырой ответ API: сохраните ответ через --save-raw."
        )
    return _validate_payload(data)


# ---------------------------------------------------------------------------
# Приведение точек прогноза к плоской структуре
# ---------------------------------------------------------------------------

def parse_point(item: dict[str, Any], tz_offset: int = 0) -> dict[str, Any]:
    """Преобразует одну точку прогноза API в плоский словарь.

    Ключи: date (dt_txt, UTC), date_local (то же время в часовом поясе города),
    temp (°C), wind_speed (м/с), wind_speed_kmh (км/ч),
    condition (weather[0].main), pop (вероятность осадков, 0..1).
    """
    main = item.get("main") or {}
    wind = item.get("wind") or {}
    weather = item.get("weather") or [{}]
    wind_speed = wind.get("speed")
    return {
        "date": item.get("dt_txt"),
        "date_local": _item_local_datetime(item, tz_offset).strftime(DATE_FORMAT),
        "temp": main.get("temp"),
        "wind_speed": wind_speed,
        "wind_speed_kmh": (
            round(wind_speed * 3.6, 2) if isinstance(wind_speed, (int, float)) else None
        ),
        "condition": weather[0].get("main"),
        "pop": item.get("pop", 0),
    }


def map_forecast(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Преобразует массив list из ответа API в список плоских точек прогноза."""
    tz_offset = _city_tz_offset(payload)
    return [parse_point(item, tz_offset) for item in payload.get("list") or []]


# ---------------------------------------------------------------------------
# Группировка по суткам в локальном времени города (поле city.timezone)
# ---------------------------------------------------------------------------

def _city_tz_offset(payload: dict[str, Any]) -> int:
    """Смещение часового пояса города в секундах (поле city.timezone)."""
    return int((payload.get("city") or {}).get("timezone") or 0)


def _item_local_datetime(item: dict[str, Any], tz_offset: int) -> datetime:
    """Локальные дата и время точки прогноза с учётом смещения часового пояса города."""
    timestamp = item.get("dt")
    if timestamp is None:
        dt_txt = item.get("dt_txt") or ""
        parsed = datetime.strptime(dt_txt, DATE_FORMAT).replace(tzinfo=timezone.utc)
        return parsed + timedelta(seconds=tz_offset)
    return datetime.fromtimestamp(timestamp + tz_offset, tz=timezone.utc)


def _item_local_date(item: dict[str, Any], tz_offset: int) -> date:
    """Локальная дата точки прогноза с учётом смещения часового пояса города."""
    return _item_local_datetime(item, tz_offset).date()


def group_points_by_day(payload: dict[str, Any]) -> dict[date, list[dict[str, Any]]]:
    """Возвращает точки прогноза, сгруппированные по локальным суткам города."""
    tz_offset = _city_tz_offset(payload)
    grouped: dict[date, list[dict[str, Any]]] = {}
    for item, point in zip(payload.get("list") or [], map_forecast(payload)):
        grouped.setdefault(_item_local_date(item, tz_offset), []).append(point)
    return grouped


def tomorrow_points(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Точки прогноза на завтра (до 8 точек с шагом 3 часа) в локальном времени города."""
    grouped = group_points_by_day(payload)
    if not grouped:
        return []
    return grouped.get(min(grouped) + timedelta(days=1), [])


def _min_value(current: float | None, value: Any) -> float | None:
    """Минимум из двух значений, устойчивый к None и нечисловым данным."""
    if not isinstance(value, (int, float)):
        return current
    return value if current is None else min(current, value)


def _max_value(current: float | None, value: Any) -> float | None:
    """Максимум из двух значений, устойчивый к None и нечисловым данным."""
    if not isinstance(value, (int, float)):
        return current
    return value if current is None else max(current, value)


def daily_summary(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Агрегирует прогноз по суткам: min/max температуры, максимум ветра и осадков."""
    tz_offset = _city_tz_offset(payload)
    days: dict[str, dict[str, Any]] = {}
    for item, point in zip(payload.get("list") or [], map_forecast(payload)):
        key = _item_local_date(item, tz_offset).isoformat()
        bucket = days.setdefault(
            key,
            {
                "date": key,
                "temp_min": None,
                "temp_max": None,
                "wind_speed_max": None,
                "wind_speed_kmh_max": None,
                "pop_max": 0,
                "conditions": [],
            },
        )
        bucket["temp_min"] = _min_value(bucket["temp_min"], point["temp"])
        bucket["temp_max"] = _max_value(bucket["temp_max"], point["temp"])
        bucket["wind_speed_max"] = _max_value(bucket["wind_speed_max"], point["wind_speed"])
        bucket["wind_speed_kmh_max"] = _max_value(
            bucket["wind_speed_kmh_max"], point["wind_speed_kmh"]
        )
        bucket["pop_max"] = _max_value(bucket["pop_max"], point["pop"]) or 0
        if point["condition"] and point["condition"] not in bucket["conditions"]:
            bucket["conditions"].append(point["condition"])
    return [days[key] for key in sorted(days)]


# ---------------------------------------------------------------------------
# Публичные функции-инструменты
# ---------------------------------------------------------------------------

def get_weather_forecast(
    city: str | None = DEFAULT_CITY,
    *,
    lat: float | None = None,
    lon: float | None = None,
    api_key: str | None = None,
    units: str = DEFAULT_UNITS,
    lang: str = DEFAULT_LANG,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Основной инструмент: прогноз города списком плоских точек.

    Возвращает до 40 точек с шагом 3 часа (5 суток) в порядке возрастания времени:
    date (UTC), date_local (время города), temp (°C), wind_speed (м/с),
    wind_speed_kmh (км/ч), condition, pop (вероятность осадков 0..1).
    """
    payload = fetch_forecast(
        city, lat=lat, lon=lon, api_key=api_key, units=units, lang=lang, timeout=timeout
    )
    return map_forecast(payload)


def get_tomorrow_forecast(
    city: str | None = DEFAULT_CITY,
    *,
    lat: float | None = None,
    lon: float | None = None,
    api_key: str | None = None,
    units: str = DEFAULT_UNITS,
    lang: str = DEFAULT_LANG,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Прогноз на завтра: до 8 точек с шагом 3 часа в локальном времени города."""
    payload = fetch_forecast(
        city, lat=lat, lon=lon, api_key=api_key, units=units, lang=lang, timeout=timeout
    )
    return tomorrow_points(payload)


# ---------------------------------------------------------------------------
# Командная строка
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Описывает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        prog="get_weather_forecast.py",
        description=(
            "Прогноз погоды OpenWeatherMap (5 суток, шаг 3 часа) в плоской структуре "
            "date / temp / wind_speed / condition / pop."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--city", default=DEFAULT_CITY, help="Город для запроса (параметр q)")
    parser.add_argument("--lat", type=float, default=None, help="Широта (используется вместе с --lon)")
    parser.add_argument("--lon", type=float, default=None, help="Долгота (используется вместе с --lat)")
    parser.add_argument(
        "--api-key",
        dest="api_key",
        default=None,
        help="Ключ OpenWeatherMap (приоритетнее переменной окружения WEATHER_API_KEY)",
    )
    parser.add_argument(
        "--units",
        default=DEFAULT_UNITS,
        choices=("metric", "imperial", "standard"),
        help="Единицы измерения (metric — °C и м/с)",
    )
    parser.add_argument("--lang", default=DEFAULT_LANG, help="Язык описаний погоды")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Таймаут запроса, секунды")
    parser.add_argument("--tomorrow", action="store_true", help="Только точки прогноза на завтра")
    parser.add_argument("--daily", action="store_true", help="Агрегированный прогноз по суткам")
    parser.add_argument(
        "--from-file",
        dest="from_file",
        default=None,
        help="Разобрать сохранённый «сырой» ответ API из файла вместо запроса в сеть",
    )
    parser.add_argument(
        "--save-raw",
        dest="save_raw",
        default=None,
        help="Сохранить «сырой» ответ API в файл (его затем можно подать в --from-file)",
    )
    parser.add_argument("--out", default=None, help="Файл для записи результата (по умолчанию — stdout)")
    parser.add_argument("--indent", type=int, default=2, help="Отступ JSON (-1 — компактный вывод)")
    return parser


def _write_json(path: str, data: Any, indent: int | None = 2) -> None:
    """Записывает данные в файл в кодировке UTF-8 (перезаписывает файл)."""
    with open(path, "w", encoding="utf-8") as file:
        file.write(json.dumps(data, ensure_ascii=False, indent=indent) + "\n")


def _configure_stdout() -> None:
    """Переключает stdout/stderr на UTF-8, чтобы русские сообщения не ломались в консоли Windows."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI. Возвращает код выхода: 0 — успех, 1 — ошибка."""
    _configure_stdout()
    args = build_parser().parse_args(argv)

    try:
        if args.from_file:
            payload = load_forecast_file(args.from_file)
        else:
            payload = fetch_forecast(
                args.city,
                lat=args.lat,
                lon=args.lon,
                api_key=args.api_key,
                units=args.units,
                lang=args.lang,
                timeout=args.timeout,
            )

        if args.save_raw:
            _write_json(args.save_raw, payload)
            print(f"Сырой ответ сохранён: {args.save_raw}", file=sys.stderr)

        if args.tomorrow:
            result: Any = tomorrow_points(payload)
            if not result:
                print("Предупреждение: в прогнозе нет точек на завтра.", file=sys.stderr)
        elif args.daily:
            result = daily_summary(payload)
        else:
            result = map_forecast(payload)
    except WeatherApiError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Ошибка чтения/записи файла: {exc}", file=sys.stderr)
        return 1

    pretty = None if args.indent < 0 else args.indent

    if args.out:
        try:
            _write_json(args.out, result, indent=pretty)
        except OSError as exc:
            print(f"Ошибка записи файла: {exc}", file=sys.stderr)
            return 1
        print(f"Готово: {args.out} (записей: {len(result)})")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=pretty))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
