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

Восход и закат берутся из ответов OpenWeatherMap (поля sunrise и sunset), а не считаются
сами: daylight_by_day() собирает их из
  * One Call 3.0 — daily[].sunrise / daily[].sunset, по суткам на 8 дней вперёд (нужна
    подписка на этот продукт для ключа; при 401 запрос просто пропускается);
  * Current weather — sys.sunrise / sys.sunset, только текущие сутки; если прогноз нужен
    на другой день, значения переносятся на него и в сообщении помечаются «данные за ДД.ММ».
В 5-дневном прогнозе (массив list) полей sunrise/sunset нет. Офлайн-расчёт (sun_times.py)
применяется только как запасной вариант, когда API не отдал солнце: так работает
source="auto"; source="calc" включает расчёт принудительно, source="api" — запрещает.

Использование как инструмента (импорт):

    from get_weather_forecast import (
        daylight_by_day, fetch_current_weather, fetch_forecast, fetch_sun_payloads,
    )

    payload = fetch_forecast("Minsk")                        # 40 точек (5 суток)
    one_call, current, warnings = fetch_sun_payloads(payload, city="Minsk")
    sun = daylight_by_day(payload, current=current, one_call=one_call)
    current = fetch_current_weather("Minsk")                 # sys.sunrise / sys.sunset

Использование из командной строки:

    python get_weather_forecast.py                       # Минск, все точки, JSON в stdout
    python get_weather_forecast.py --tomorrow            # только точки на завтра
    python get_weather_forecast.py --daily               # агрегированный прогноз по суткам
    python get_weather_forecast.py --sun                 # восход и закат по суткам (из API)
    python get_weather_forecast.py --city Minsk --out forecast.json
    python get_weather_forecast.py --save-raw response.json              # сохранить сырой ответ API
    python get_weather_forecast.py --save-raw-sun sun.json               # сохранить ответы о солнце
    python get_weather_forecast.py --from-file response.json --sun-file sun.json --sun

Ключ API берётся из переменной окружения WEATHER_API_KEY (в GitHub Actions — из секрета
с тем же именем), поддерживается и старое имя OPENWEATHER_API_KEY, а также аргумент
--api-key. В коде ключ не хранится.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import requests

from sun_times import Daylight, MINUTES_PER_DAY, sun_times_by_day

BASE_URL = "https://api.openweathermap.org/data/2.5/forecast"
CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"       # sys.sunrise/sunset
ONE_CALL_URL = "https://api.openweathermap.org/data/3.0/onecall"      # daily[].sunrise/sunset
ONE_CALL_EXCLUDE = "minutely,hourly,alerts"   # нужны только сутки: восход и закат
SECONDS_PER_DAY = MINUTES_PER_DAY * 60
SUN_SOURCES = ("auto", "api", "calc")   # auto — API, иначе расчёт; api — только API; calc — только расчёт
SUN_FILE_KEYS = ("one_call", "current")  # ключи файла, который пишет save_sun_payloads()

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


def _request_json(
    url: str,
    params: Mapping[str, Any],
    *,
    timeout: float,
    session: Any = None,
    kind: str = "прогноз",
    city_hint: str | None = None,
    unauthorized: str | None = None,
) -> dict[str, Any]:
    """Запрашивает JSON у OpenWeatherMap и объясняет ошибки понятным текстом."""
    http = session or requests
    try:
        response = http.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise WeatherApiError(f"Не удалось обратиться к OpenWeatherMap ({kind}): {exc}") from exc

    if response.status_code == 401:
        raise WeatherApiError(
            unauthorized or "OpenWeatherMap отклонил ключ API (HTTP 401): проверьте ключ."
        )
    if response.status_code == 404:
        raise WeatherApiError(
            f"Город не найден (HTTP 404): {city_hint}."
            if city_hint
            else f"OpenWeatherMap не нашёл данные ({kind}, HTTP 404)."
        )
    if response.status_code == 429:
        raise WeatherApiError("Превышен лимит запросов к OpenWeatherMap (HTTP 429).")
    if response.status_code != 200:
        raise WeatherApiError(
            f"OpenWeatherMap вернул HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise WeatherApiError("Ответ OpenWeatherMap не является корректным JSON.") from exc
    if not isinstance(data, dict):
        raise WeatherApiError("Ответ OpenWeatherMap не является объектом JSON.")
    return data


def _city_hint(params: Mapping[str, Any]) -> str:
    """Подсказка для сообщения об ошибке: город или координаты из параметров запроса."""
    return str(params.get("q") or f"{params.get('lat')}, {params.get('lon')}")


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
    data = _request_json(
        BASE_URL,
        params,
        timeout=timeout,
        session=session,
        kind="прогноз на 5 суток",
        city_hint=_city_hint(params),
    )
    return _validate_payload(data)


def fetch_current_weather(
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
    """Текущая погода (Current weather API): нужна для полей sys.sunrise и sys.sunset."""
    params = build_params(city, lat, lon, api_key, units, lang)
    data = _request_json(
        CURRENT_URL,
        params,
        timeout=timeout,
        session=session,
        kind="текущая погода",
        city_hint=_city_hint(params),
    )
    if not isinstance(data.get("sys"), dict):
        raise WeatherApiError(
            "В ответе Current weather нет блока sys с восходом и закатом (sunrise/sunset)."
        )
    return data


def fetch_one_call(
    lat: float,
    lon: float,
    *,
    api_key: str | None = None,
    units: str = DEFAULT_UNITS,
    lang: str = DEFAULT_LANG,
    timeout: float = DEFAULT_TIMEOUT,
    session: Any = None,
) -> dict[str, Any]:
    """One Call 3.0: восход и закат на каждые сутки (daily[].sunrise / daily[].sunset).

    Для ключа нужна подписка на продукт One Call 3.0: без неё OpenWeatherMap отвечает
    HTTP 401 — тогда вызывающий код берёт солнце из Current weather (sys.sunrise/sunset).
    """
    params = build_params(None, lat, lon, api_key, units, lang)
    params["exclude"] = ONE_CALL_EXCLUDE
    data = _request_json(
        ONE_CALL_URL,
        params,
        timeout=timeout,
        session=session,
        kind="One Call 3.0",
        city_hint=f"{lat}, {lon}",
        unauthorized=(
            "One Call 3.0 недоступен для ключа (HTTP 401): подписка на этот продукт не "
            "оформлена — восход и закат возьмутся из Current weather (sys.sunrise/sunset)."
        ),
    )
    if not data.get("daily"):
        raise WeatherApiError(
            "В ответе One Call 3.0 нет массива daily с восходом и закатом (sunrise/sunset)."
        )
    return data


def fetch_sun_payloads(
    payload: dict[str, Any],
    *,
    city: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    api_key: str | None = None,
    units: str = DEFAULT_UNITS,
    lang: str = DEFAULT_LANG,
    timeout: float = DEFAULT_TIMEOUT,
    source: str = "auto",
    session: Any = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    """Запрашивает у OpenWeatherMap данные о солнце: (one_call, current, предупреждения).

    One Call 3.0 отдаёт восход и закат на каждые сутки (нужна подписка для ключа),
    Current weather — только на текущие сутки; в 5-дневном прогнозе этих полей нет,
    поэтому солнце берётся отсюда. Координаты для One Call берутся из ответа прогноза
    (city.coord), запрос Current weather идёт по тем же city/lat/lon, что и прогноз.
    При source="calc" запросы не делаются вовсе, при ошибке — None и текст для лога.
    """
    if source == "calc":
        return None, None, []

    warnings: list[str] = []
    one_call: dict[str, Any] | None = None
    location = city_location(payload)
    if location is not None:
        try:
            one_call = fetch_one_call(
                location[0],
                location[1],
                api_key=api_key,
                units=units,
                lang=lang,
                timeout=timeout,
                session=session,
            )
        except WeatherApiError as exc:
            warnings.append(str(exc))

    current: dict[str, Any] | None = None
    try:
        current = fetch_current_weather(
            city,
            lat=lat,
            lon=lon,
            api_key=api_key,
            units=units,
            lang=lang,
            timeout=timeout,
            session=session,
        )
    except WeatherApiError as exc:
        warnings.append(str(exc))

    return one_call, current, warnings




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


def sun_payload_kind(data: Mapping[str, Any]) -> str | None:
    """Тип сохранённого ответа с солнцем: 'combined' (--save-raw-sun), 'onecall', 'current'."""
    if any(key in data for key in SUN_FILE_KEYS):
        return "combined"
    if isinstance(data.get("daily"), list) or "timezone_offset" in data:
        return "onecall"
    if isinstance((data.get("sys") or {}).get("sunrise"), (int, float)):
        return "current"
    return None


def load_sun_files(
    paths: Sequence[str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Читает файлы с данными о солнце (--sun-file): (one_call, current); чего нет — None.

    Подходит любой из ответов API: Current weather (sys.sunrise/sunset), One Call 3.0
    (daily[].sunrise/sunset) или общий файл, который пишет --save-raw-sun.
    """
    one_call: dict[str, Any] | None = None
    current: dict[str, Any] | None = None
    for path in paths:
        with open(path, encoding="utf-8") as file:
            try:
                data = json.load(file)
            except json.JSONDecodeError as exc:
                raise WeatherApiError(f"Файл {path} не является корректным JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise WeatherApiError(f"Файл {path}: ожидается объект JSON с ответом API.")

        kind = sun_payload_kind(data)
        if kind == "combined":
            one_call = data.get("one_call") or one_call
            current = data.get("current") or current
        elif kind == "onecall":
            one_call = data
        elif kind == "current":
            current = data
        else:
            raise WeatherApiError(
                f"В файле {path} нет полей sunrise/sunset: нужен ответ Current weather, "
                "One Call 3.0 или файл, сохранённый через --save-raw-sun."
            )
    return one_call, current


def save_sun_payloads(
    path: str,
    *,
    one_call: dict[str, Any] | None = None,
    current: dict[str, Any] | None = None,
) -> None:
    """Сохраняет ответы API с солнцем в один файл — его потом читает --sun-file."""
    _write_json(path, {"one_call": one_call, "current": current})


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


def daily_summary(
    payload: dict[str, Any],
    *,
    current: dict[str, Any] | None = None,
    one_call: dict[str, Any] | None = None,
    source: str = "auto",
) -> list[dict[str, Any]]:
    """Агрегирует прогноз по суткам: min/max температуры, максимум ветра и осадков.

    К суткам добавляются восход и закат (ключи sunrise / sunset в формате ЧЧ:ММ) из
    данных OpenWeatherMap — по ним ограничивается катание (см. daylight_by_day).
    """
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

    summary = [days[key] for key in sorted(days)]
    daylight = daylight_by_day(payload, current=current, one_call=one_call, source=source)
    for bucket in summary:
        sun = daylight.get(bucket["date"])
        if sun is not None:
            bucket["sunrise"] = sun.sunrise_text
            bucket["sunset"] = sun.sunset_text
            bucket["sun_source"] = sun.source_text
    return summary


# ---------------------------------------------------------------------------
# Восход и закат из данных OpenWeatherMap (поля sunrise / sunset)
# ---------------------------------------------------------------------------

def city_location(payload: dict[str, Any]) -> tuple[float, float] | None:
    """Координаты города из ответа API: (широта, долгота) или None, если их нет."""
    coord = (payload.get("city") or {}).get("coord") or {}
    lat, lon = coord.get("lat"), coord.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)
    return None


def _parse_timestamp(text: Any) -> datetime | None:
    """Разбирает время точки прогноза 'ГГГГ-ММ-ДД ЧЧ:ММ:СС' (None при ошибке)."""
    try:
        return datetime.strptime(str(text), DATE_FORMAT)
    except (TypeError, ValueError):
        return None


def city_tz_hours(
    payload: dict[str, Any],
    points: Sequence[dict[str, Any]] | None = None,
) -> float | None:
    """Смещение часового пояса города в часах: city.timezone или разница date/date_local."""
    raw = (payload.get("city") or {}).get("timezone")
    if isinstance(raw, (int, float)):
        return raw / 3600.0
    for point in points if points is not None else map_forecast(payload):
        utc = _parse_timestamp(point.get("date"))
        local = _parse_timestamp(point.get("date_local"))
        if utc is not None and local is not None:
            return (local - utc).total_seconds() / 3600.0
    return None


def _days_of(points: Sequence[dict[str, Any]]) -> set[date]:
    """Локальные даты точек прогноза (нужны, если в ответе нет массива list)."""
    days: set[date] = set()
    for point in points:
        text = (point.get("date_local") or point.get("date") or "")[:10]
        try:
            days.add(date.fromisoformat(text))
        except ValueError:
            continue
    return days


def _api_tz_offset(payload: dict[str, Any]) -> int:
    """Смещение часового пояса в секундах: timezone_offset (One Call) или timezone (Current)."""
    raw = payload.get("timezone_offset")
    if not isinstance(raw, (int, float)):
        raw = payload.get("timezone")
    return int(raw) if isinstance(raw, (int, float)) else 0


def _sun_minutes(timestamp: Any, tz_offset: int) -> int | None:
    """Минуты от местной полуночи для отметки времени (unix) из ответа API."""
    if not isinstance(timestamp, (int, float)):
        return None
    return int((int(timestamp) + tz_offset) % SECONDS_PER_DAY) // 60


def _sun_local_date(timestamp: Any, tz_offset: int) -> date | None:
    """Локальная дата отметки времени (unix) из ответа API."""
    if not isinstance(timestamp, (int, float)):
        return None
    return datetime.fromtimestamp(int(timestamp) + tz_offset, tz=timezone.utc).date()


def _make_daylight(
    day: str,
    sunrise: Any,
    sunset: Any,
    tz_offset: int,
    source: str,
) -> Daylight | None:
    """Собирает Daylight из пары отметок времени API (None, если данные непригодны).

    Непригодны отсутствующие отметки и закат раньше восхода — так бывает в полярных
    широтах, где OpenWeatherMap эти поля не отдаёт; тогда день считается без солнца.
    """
    rise = _sun_minutes(sunrise, tz_offset)
    down = _sun_minutes(sunset, tz_offset)
    if rise is None or down is None or rise >= down:
        return None
    return Daylight(day, rise, down, source=source)


def daylight_from_current(payload: dict[str, Any]) -> dict[str, Daylight]:
    """Восход и закат из Current weather API: sys.sunrise и sys.sunset (текущие сутки)."""
    block = payload.get("sys") or {}
    tz_offset = _api_tz_offset(payload)
    day = _sun_local_date(block.get("sunrise"), tz_offset)
    if day is None:
        return {}
    item = _make_daylight(
        day.isoformat(), block.get("sunrise"), block.get("sunset"), tz_offset, "current"
    )
    return {} if item is None else {day.isoformat(): item}


def daylight_from_one_call(payload: dict[str, Any]) -> dict[str, Daylight]:
    """Восход и закат по суткам из One Call 3.0: daily[].sunrise и daily[].sunset."""
    tz_offset = _api_tz_offset(payload)
    result: dict[str, Daylight] = {}
    for item in payload.get("daily") or []:
        if not isinstance(item, dict):
            continue
        day = _sun_local_date(item.get("dt", item.get("sunrise")), tz_offset)
        if day is None:
            continue
        daylight = _make_daylight(
            day.isoformat(), item.get("sunrise"), item.get("sunset"), tz_offset, "onecall"
        )
        if daylight is not None:
            result[day.isoformat()] = daylight
    return result


def _nearest_day(known: Mapping[str, Daylight], day: date) -> str | None:
    """Ближайшая к указанным суткам известная дата API (сначала по разнице, потом по дате)."""
    if not known:
        return None
    return min(known, key=lambda text: (abs((date.fromisoformat(text) - day).days), text))


def _borrowed(daylight: Daylight, day: date) -> Daylight:
    """Переносит известные значения API на другие сутки (светлое время меняется на минуты)."""
    return replace(
        daylight,
        day=day.isoformat(),
        borrowed_from=daylight.borrowed_from or daylight.day,
    )


def _calculated_days(days: Sequence[date], payload: dict[str, Any]) -> dict[str, Daylight]:
    """Запасной вариант: восход и закат считает sun_times.py по координатам и поясу города."""
    location = city_location(payload)
    tz_hours = city_tz_hours(payload)
    if location is None or tz_hours is None:
        return {}
    lat, lon = location
    return sun_times_by_day(days, lat, lon, tz_hours)


def daylight_by_day(
    payload: dict[str, Any],
    points: Sequence[dict[str, Any]] | None = None,
    *,
    current: dict[str, Any] | None = None,
    one_call: dict[str, Any] | None = None,
    source: str = "auto",
) -> dict[str, Daylight]:
    """Восход и закат каждого дня прогноза: {ISO-дата: Daylight}.

    Данные берутся из ответов OpenWeatherMap (поля sunrise/sunset): daily[] в One Call 3.0
    (на каждые сутки) и sys в Current weather (текущие сутки). Если нужных суток в API нет,
    берутся значения ближайших — это видно как Daylight.borrowed_from, а в сообщении как
    «данные за ДД.ММ»: ограничение по светлому времени из-за разницы в пару минут не страдает.

    Если солнца в ответах API нет вовсе, дни считаются офлайн-расчётом sun_times.py — но
    только при source="auto" или "calc"; при source="api" такой день остаётся без солнца
    (вердикт считается по резервному интервалу часов). Пустой словарь означает, что строк
    про светлое время в сообщении не будет.
    """
    if source not in SUN_SOURCES:
        raise WeatherApiError(
            f"Неизвестный источник солнца {source!r}: ожидается {' / '.join(SUN_SOURCES)}."
        )

    days = sorted(set(group_points_by_day(payload)) or _days_of(points or []))
    if not days:
        return {}

    known: dict[str, Daylight] = {}
    if source != "calc":
        if one_call:
            known.update(daylight_from_one_call(one_call))
        if current:
            known.update(daylight_from_current(current))

    result: dict[str, Daylight] = {}
    missing: list[date] = []
    for day in days:
        exact = known.get(day.isoformat())
        if exact is not None:
            result[day.isoformat()] = exact
            continue
        donor = _nearest_day(known, day)
        if donor is None:
            missing.append(day)
        else:
            result[day.isoformat()] = _borrowed(known[donor], day)

    if missing and source != "api":
        result.update(_calculated_days(missing, payload))
    return result



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

def _sun_data(
    args: argparse.Namespace,
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    """Данные о солнце для CLI: из файлов --sun-file или запросом к API."""
    if args.sun_files:
        one_call, current = load_sun_files(args.sun_files)
        return one_call, current, []
    return fetch_sun_payloads(
        payload,
        city=args.city,
        lat=args.lat,
        lon=args.lon,
        api_key=args.api_key,
        units=args.units,
        lang=args.lang,
        timeout=args.timeout,
        source=args.sun_source,
    )


def sun_report(daylight: Mapping[str, Daylight]) -> dict[str, dict[str, Any]]:
    """Восход и закат по суткам в плоском виде (для --sun и JSON-вывода)."""
    return {
        day: {
            "sunrise": item.sunrise_text,
            "sunset": item.sunset_text,
            "daylight_minutes": item.duration,
            "source": item.source_text,
            "borrowed_from": item.borrowed_from or None,
        }
        for day, item in daylight.items()
    }


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
        "--sun",
        action="store_true",
        help="Показать восход и закат по суткам прогноза (данные OpenWeatherMap)",
    )
    parser.add_argument(
        "--sun-source",
        dest="sun_source",
        default="auto",
        choices=SUN_SOURCES,
        help=(
            "Откуда брать восход и закат: auto — поля sunrise/sunset из API, при их "
            "отсутствии расчёт; api — только данные API; calc — только расчёт"
        ),
    )
    parser.add_argument(
        "--sun-file",
        dest="sun_files",
        action="append",
        default=[],
        metavar="FILE",
        help="Файл с ответом API о солнце (Current weather, One Call 3.0 или --save-raw-sun)",
    )
    parser.add_argument(
        "--save-raw-sun",
        dest="save_raw_sun",
        default=None,
        help="Сохранить ответы API с восходом и закатом в один файл (для --sun-file)",
    )
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

        one_call: dict[str, Any] | None = None
        current: dict[str, Any] | None = None
        if args.sun or args.daily or args.save_raw_sun:
            one_call, current, warnings = _sun_data(args, payload)
            for warning in warnings:
                print(f"Предупреждение: {warning}", file=sys.stderr)
        if args.save_raw_sun:
            save_sun_payloads(args.save_raw_sun, one_call=one_call, current=current)
            print(f"Ответы API с солнцем сохранены: {args.save_raw_sun}", file=sys.stderr)

        if args.sun:
            result: Any = sun_report(
                daylight_by_day(
                    payload, current=current, one_call=one_call, source=args.sun_source
                )
            )
            if not result:
                print(
                    "Предупреждение: в данных API нет полей sunrise/sunset, "
                    "восход и закат определить не удалось.",
                    file=sys.stderr,
                )
        elif args.tomorrow:
            result = tomorrow_points(payload)
            if not result:
                print("Предупреждение: в прогнозе нет точек на завтра.", file=sys.stderr)
        elif args.daily:
            result = daily_summary(payload, current=current, one_call=one_call, source=args.sun_source)
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
