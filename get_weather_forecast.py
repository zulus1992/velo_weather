# -*- coding: utf-8 -*-
"""Инструмент получения прогноза погоды для ассистента-райдера.

Источник данных: WeatherAPI.com, метод forecast.json — почасовой прогноз с шагом 1 час.
Каждая точка прогноза приводится к плоской структуре:

    {
        "date": "2026-09-21 09:00:00",        # time_epoch — время точки прогноза (UTC)
        "date_local": "2026-09-21 12:00:00",  # hour.time — то же время в поясе города
        "temp": 14.0,                         # hour.temp_c, °C
        "wind_speed": 3.26,                   # hour.wind_kph / 3.6, м/с
        "wind_speed_kmh": 11.7,               # hour.wind_kph — правила заданы в км/ч
        "condition": "Облачно",               # hour.condition.text (lang=ru)
        "pop": 0.2                            # вероятность осадков, 0..1
    }

Прогноз почасовой: на каждые сутки 24 точки (00:00…23:00) в местном времени города.
Бесплатный тариф WeatherAPI.com отдаёт 3 суток (72 точки), платный — до 14 суток;
сколько суток запрашивать, задаёт параметр days (--days).

Восход и закат берутся из того же ответа API, а не считаются сами: daylight_by_day()
читает поля forecast.forecastday[].astro.sunrise / astro.sunset для каждых суток
прогноза. Если значения непригодны (полярные широты и прочее), применяется
офлайн-расчёт (sun_times.py) — так работает source="auto"; source="calc" включает
расчёт принудительно, source="api" запрещает.

Использование как инструмента (импорт):

    from get_weather_forecast import daylight_by_day, fetch_forecast, get_weather_forecast

    payload = fetch_forecast("Minsk")           # сырой ответ WeatherAPI (3 суток)
    points = map_forecast(payload)              # до 72 почасовых точек
    points = get_weather_forecast("Minsk")      # то же самое одним вызовом
    sun = daylight_by_day(payload)              # восход и закат по суткам
    points = get_tomorrow_forecast("Minsk")     # 24 точки на завтра

Использование из командной строки:

    python get_weather_forecast.py                       # Минск, все точки, JSON в stdout
    python get_weather_forecast.py --tomorrow            # только точки на завтра
    python get_weather_forecast.py --daily               # агрегированный прогноз по суткам
    python get_weather_forecast.py --sun                 # восход и закат по суткам (из API)
    python get_weather_forecast.py --days 5 --city Minsk --out forecast.json
    python get_weather_forecast.py --save-raw response.json              # сохранить сырой ответ
    python get_weather_forecast.py --from-file response.json --sun       # разбор офлайн

Ключ WeatherAPI.com берётся из переменной окружения WEATHER_API_KEY (в GitHub Actions —
из секрета с тем же именем), а также аргументом --api-key. В коде ключ не хранится.
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

BASE_URL = "https://api.weatherapi.com/v1/forecast.json"

DEFAULT_CITY = "Minsk"       # город по умолчанию (параметр q)
DEFAULT_LANG = "ru"          # lang=ru — описания погоды на русском
DEFAULT_DAYS = 3             # суток прогноза по умолчанию (бесплатный тариф: 3)
MAX_DAYS = 14                # предел параметра days у WeatherAPI
DEFAULT_TIMEOUT = 15.0
SECONDS_PER_DAY = MINUTES_PER_DAY * 60
SUN_SOURCES = ("auto", "api", "calc")   # auto — API, иначе расчёт; api — только API; calc — только расчёт
POP_PERCENT = 100.0          # chance_of_rain / chance_of_snow приходят в процентах (0..100)

DATE_FORMAT = "%Y-%m-%d"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
TIME_INPUT_FORMAT = "%Y-%m-%d %H:%M"     # формат hour.time и location.localtime
CLOCK_FORMATS = ("%I:%M %p", "%I:%M%p", "%I:%M:%S %p", "%H:%M:%S", "%H:%M")  # astro: "05:12 AM" или "05:12"

# Ключ WeatherAPI.com берётся из переменной окружения WEATHER_API_KEY
# (в GitHub Actions — из секрета WEATHER_API_KEY, см. .github/workflows/weather.yml).
# В коде ключ не хранится: если переменная не задана, скрипт скажет об этом понятной ошибкой.
DEFAULT_API_KEY = os.getenv("WEATHER_API_KEY", "")

# Понятные подсказки к кодам ошибок WeatherAPI (поле error.code).
ERROR_HINTS = {
    1002: "не передан ключ API: задайте --api-key или переменную окружения WEATHER_API_KEY",
    1003: "не передан параметр q (город или координаты)",
    1005: "некорректный URL запроса",
    1006: "город не найден: проверьте --city либо --lat с --lon",
    1007: "дата запроса выходит за разрешённый диапазон",
    2006: "ключ API недействителен: проверьте секрет WEATHER_API_KEY",
    2007: "у ключа закончился лимит вызовов: квота за месяц исчерпана",
    2008: "ключ API отключён",
    2009: "у ключа нет доступа к этому продукту: проверьте тариф",
}


class WeatherApiError(RuntimeError):
    """Ошибка запроса к WeatherAPI.com или разбора его ответа."""


def _clean_key(value: Any) -> str:
    """Убирает пробелы и кавычки — частая ошибка при вставке ключа или секрета."""
    return str(value or "").strip().strip("'\"").strip()


def get_api_key(api_key: str | None = None) -> str:
    """Возвращает ключ API: аргумент -> WEATHER_API_KEY из окружения."""
    key = _clean_key(api_key) or _clean_key(os.getenv("WEATHER_API_KEY")) or _clean_key(DEFAULT_API_KEY)
    if not key or key == "YOUR_API_KEY":
        raise WeatherApiError(
            "Не задан ключ WeatherAPI.com: укажите --api-key или задайте переменную "
            "окружения WEATHER_API_KEY (в GitHub Actions — секрет WEATHER_API_KEY)."
        )
    return key


def query_text(
    city: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> str:
    """Значение параметра q: 'широта,долгота' (если заданы оба) или название города."""
    if lat is not None and lon is not None:
        return f"{lat},{lon}"
    return city or DEFAULT_CITY


def check_days(days: int) -> int:
    """Проверяет число суток прогноза: WeatherAPI принимает 1…14, бесплатный тариф — 3."""
    try:
        value = int(days)
    except (TypeError, ValueError) as exc:
        raise WeatherApiError(
            f"Число суток прогноза должно быть целым числом (получено {days!r})."
        ) from exc
    if not 1 <= value <= MAX_DAYS:
        raise WeatherApiError(
            f"Число суток прогноза должно быть в диапазоне 1…{MAX_DAYS} (получено {value})."
        )
    return value


def build_params(
    city: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    api_key: str | None = None,
    lang: str = DEFAULT_LANG,
    days: int = DEFAULT_DAYS,
) -> dict[str, Any]:
    """Собирает query-параметры: ключ, q (город или координаты), lang, days, aqi, alerts."""
    return {
        "key": get_api_key(api_key),
        "q": query_text(city, lat, lon),
        "days": check_days(days),
        "lang": lang or DEFAULT_LANG,
        "aqi": "no",
        "alerts": "no",
    }


def _error_text(data: Mapping[str, Any]) -> str | None:
    """Текст ошибки из ответа WeatherAPI: {"error": {"code": …, "message": …}}."""
    error = data.get("error")
    if not isinstance(error, Mapping):
        return None
    code = error.get("code")
    message = str(error.get("message") or "").strip() or "описание не указано"
    if code is None:
        return message
    try:
        hint = ERROR_HINTS.get(int(code))
    except (TypeError, ValueError):
        hint = None
    return f"код {code}: {message}" + (f" ({hint})" if hint else "")


def _request_json(
    url: str,
    params: Mapping[str, Any],
    *,
    timeout: float,
    session: Any = None,
    kind: str = "прогноз",
    city_hint: str | None = None,
) -> dict[str, Any]:
    """Запрашивает JSON у WeatherAPI.com и объясняет ошибки понятным текстом."""
    http = session or requests
    try:
        response = http.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise WeatherApiError(f"Не удалось обратиться к WeatherAPI.com ({kind}): {exc}") from exc

    try:
        data: Any = response.json()
    except ValueError:
        data = None

    # WeatherAPI кладёт причину ошибки в тело ответа (error.code / error.message),
    # поэтому проверяем его раньше HTTP-кода: так видно, что именно не понравилось.
    if isinstance(data, dict):
        text = _error_text(data)
        if text is not None:
            raise WeatherApiError(f"WeatherAPI.com вернул ошибку ({kind}): {text}.")

    if response.status_code == 401:
        raise WeatherApiError(
            "WeatherAPI.com отклонил ключ API (HTTP 401): проверьте ключ в WEATHER_API_KEY."
        )
    if response.status_code == 403:
        raise WeatherApiError(
            "WeatherAPI.com отказал в доступе (HTTP 403): проверьте тариф и лимиты ключа."
        )
    if response.status_code == 429:
        raise WeatherApiError("Превышен лимит запросов к WeatherAPI.com (HTTP 429).")
    if response.status_code != 200:
        hint = f" Проверьте город или координаты: {city_hint}." if city_hint else ""
        raise WeatherApiError(
            f"WeatherAPI.com вернул HTTP {response.status_code}: {response.text[:200]}.{hint}"
        )
    if not isinstance(data, dict):
        raise WeatherApiError("Ответ WeatherAPI.com не является объектом JSON.")
    return data


def fetch_forecast(
    city: str | None = None,
    *,
    lat: float | None = None,
    lon: float | None = None,
    api_key: str | None = None,
    lang: str = DEFAULT_LANG,
    days: int = DEFAULT_DAYS,
    timeout: float = DEFAULT_TIMEOUT,
    session: Any = None,
) -> dict[str, Any]:
    """Запрашивает почасовой прогноз у WeatherAPI.com и возвращает ответ API «как есть».

    Ответ содержит блок location (город, координаты, часовой пояс), почасовые точки
    forecast.forecastday[].hour[] и восход с закатом в forecast.forecastday[].astro.
    """
    params = build_params(city, lat=lat, lon=lon, api_key=api_key, lang=lang, days=days)
    data = _request_json(
        BASE_URL,
        params,
        timeout=timeout,
        session=session,
        kind="прогноз по часам",
        city_hint=str(params.get("q") or ""),
    )
    return _validate_payload(data)


def _validate_payload(data: Any) -> dict[str, Any]:
    """Проверяет структуру ответа WeatherAPI (location + forecast.forecastday[].hour)."""
    if not isinstance(data, dict):
        raise WeatherApiError("Ответ WeatherAPI.com не является объектом JSON.")
    text = _error_text(data)
    if text is not None:
        raise WeatherApiError(f"WeatherAPI.com вернул ошибку: {text}.")
    if not isinstance(data.get("location"), dict):
        raise WeatherApiError(
            "В ответе WeatherAPI нет блока location с городом, координатами и часовым поясом."
        )
    if not hour_items(data):
        raise WeatherApiError(
            "В ответе WeatherAPI нет почасового прогноза (forecast.forecastday[].hour)."
        )
    return data


def load_forecast_file(path: str) -> dict[str, Any]:
    """Читает сохранённый «сырой» ответ API из файла (офлайн-разбор без обращения к сети).

    Ожидает ответ WeatherAPI.com целиком (с ключами location и forecast) — например,
    файл, сохранённый командой `--save-raw response.json`. Восход и закат берутся из
    того же файла (поля astro), отдельный файл с солнцем не нужен.
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
# Приведение почасовых точек к плоской структуре
# ---------------------------------------------------------------------------

def hour_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Часовые точки прогноза из ответа: forecast.forecastday[].hour[] (шаг 1 час)."""
    items: list[dict[str, Any]] = []
    for day in (payload.get("forecast") or {}).get("forecastday") or []:
        if not isinstance(day, Mapping):
            continue
        for item in day.get("hour") or []:
            if isinstance(item, Mapping):
                items.append(dict(item))
    return items


def _parse_hour_time(text: Any) -> datetime | None:
    """Разбирает местное время точки прогноза 'ГГГГ-ММ-ДД ЧЧ:ММ' (None при ошибке)."""
    value = str(text or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value[:16], TIME_INPUT_FORMAT)
    except ValueError:
        return None


def _parse_timestamp(text: Any) -> datetime | None:
    """Разбирает время в формате 'ГГГГ-ММ-ДД ЧЧ:ММ:СС' (None при ошибке)."""
    try:
        return datetime.strptime(str(text), TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def parse_clock(text: Any) -> int | None:
    """Время из поля astro в минутах от местной полуночи (None, если времени нет).

    WeatherAPI отдаёт время строкой — '05:12 AM' (12-часовой формат) или '05:12'
    (24-часовой). В полярных широтах вместо времени приходит пустое значение —
    тогда None, а восход и закат считает офлайн-расчёт sun_times.py.
    """
    value = str(text or "").strip()
    if not value:
        return None
    for fmt in CLOCK_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return parsed.hour * 60 + parsed.minute
    return None


def _percent(value: Any) -> float:
    """Приводит вероятность из ответа API (0..100) к числу; неизвестное значение — 0."""
    return float(value) if isinstance(value, (int, float)) else 0.0


def parse_point(item: Mapping[str, Any], tz_offset: int = 0) -> dict[str, Any]:
    """Преобразует одну часовую точку прогноза API в плоский словарь.

    Ключи: date (время точки в UTC — time_epoch), date_local (время города — hour.time),
    temp (°C), wind_speed (м/с), wind_speed_kmh (км/ч), condition (hour.condition.text),
    pop — вероятность осадков 0..1 (максимум из chance_of_rain и chance_of_snow).
    """
    wind_kph = item.get("wind_kph")
    local = _parse_hour_time(item.get("time"))
    epoch = item.get("time_epoch")
    if isinstance(epoch, (int, float)):
        utc = datetime.fromtimestamp(int(epoch), tz=timezone.utc).replace(tzinfo=None)
    elif local is not None:
        utc = local - timedelta(seconds=tz_offset)
    else:
        utc = None
    if local is None and utc is not None:
        local = utc + timedelta(seconds=tz_offset)
    return {
        "date": utc.strftime(TIME_FORMAT) if utc is not None else None,
        "date_local": local.strftime(TIME_FORMAT) if local is not None else None,
        "temp": item.get("temp_c"),
        "wind_speed": round(wind_kph / 3.6, 2) if isinstance(wind_kph, (int, float)) else None,
        "wind_speed_kmh": wind_kph if isinstance(wind_kph, (int, float)) else None,
        "condition": (item.get("condition") or {}).get("text"),
        "pop": round(
            max(_percent(item.get("chance_of_rain")), _percent(item.get("chance_of_snow")))
            / POP_PERCENT,
            4,
        ),
    }


def map_forecast(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Преобразует почасовой прогноз из ответа API в список плоских точек."""
    tz_offset = _city_tz_offset(payload)
    return [parse_point(item, tz_offset) for item in hour_items(payload)]


# ---------------------------------------------------------------------------
# Часовой пояс города и группировка точек по суткам
# ---------------------------------------------------------------------------

def _offset_from(local: datetime, epoch: float) -> int:
    """Разница между местным временем и UTC-отметкой в секундах, округлённая до минут."""
    utc = datetime.fromtimestamp(int(epoch), tz=timezone.utc).replace(tzinfo=None)
    return int(round((local - utc).total_seconds() / 60.0)) * 60


def _city_tz_offset(payload: Mapping[str, Any]) -> int:
    """Смещение часового пояса города в секундах: localtime против localtime_epoch."""
    location = payload.get("location") or {}
    local = _parse_hour_time(location.get("localtime"))
    epoch = location.get("localtime_epoch")
    if local is not None and isinstance(epoch, (int, float)):
        return _offset_from(local, float(epoch))
    # Запасной путь: сравниваем локальное время первой часовой точки с её time_epoch.
    for item in hour_items(payload):
        local = _parse_hour_time(item.get("time"))
        epoch = item.get("time_epoch")
        if local is not None and isinstance(epoch, (int, float)):
            return _offset_from(local, float(epoch))
    return 0


def city_location(payload: Mapping[str, Any]) -> tuple[float, float] | None:
    """Координаты города из ответа API (location.lat / location.lon) или None."""
    location = payload.get("location") or {}
    lat, lon = location.get("lat"), location.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)
    return None


def city_tz_hours(
    payload: Mapping[str, Any],
    points: Sequence[Mapping[str, Any]] | None = None,
) -> float | None:
    """Смещение часового пояса города в часах (по ответу API или по точкам прогноза)."""
    location = payload.get("location") or {}
    local = _parse_hour_time(location.get("localtime"))
    if local is not None and isinstance(location.get("localtime_epoch"), (int, float)):
        return _city_tz_offset(payload) / 3600.0
    source = points if points is not None else map_forecast(payload)
    for point in source:
        utc = _parse_timestamp(point.get("date"))
        city_time = _parse_timestamp(point.get("date_local"))
        if utc is not None and city_time is not None:
            return (city_time - utc).total_seconds() / 3600.0
    return None


def _point_local_date(point: Mapping[str, Any]) -> date | None:
    """Локальная дата точки прогноза (date_local, а при его отсутствии — date)."""
    text = str(point.get("date_local") or point.get("date") or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def group_points_by_day(payload: Mapping[str, Any]) -> dict[date, list[dict[str, Any]]]:
    """Возвращает точки прогноза, сгруппированные по суткам в поясе города."""
    grouped: dict[date, list[dict[str, Any]]] = {}
    for point in map_forecast(payload):
        day = _point_local_date(point)
        if day is not None:
            grouped.setdefault(day, []).append(point)
    return grouped


def tomorrow_points(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Точки прогноза на завтра (24 часа с шагом 1 час) в местном времени города."""
    grouped = group_points_by_day(payload)
    if not grouped:
        return []
    return grouped.get(min(grouped) + timedelta(days=1), [])


# ---------------------------------------------------------------------------
# Агрегированный прогноз по суткам
# ---------------------------------------------------------------------------

def _min_value(current: Any, value: Any) -> Any:
    """Минимум двух значений, где None означает «данных нет»."""
    if value is None:
        return current
    return value if current is None else min(current, value)


def _max_value(current: Any, value: Any) -> Any:
    """Максимум двух значений, где None означает «данных нет»."""
    if value is None:
        return current
    return value if current is None else max(current, value)


def daily_summary(
    payload: Mapping[str, Any],
    *,
    source: str = "auto",
) -> list[dict[str, Any]]:
    """Сводка по суткам: температура, ветер, осадки, описания погоды и светлое время.

    К каждым суткам добавляются восход и закат (ключи sunrise / sunset в формате ЧЧ:ММ)
    из полей astro ответа WeatherAPI — по ним и ограничивается катание.
    """
    days: dict[str, dict[str, Any]] = {}
    for point in map_forecast(payload):
        day_text = str(point.get("date_local") or point.get("date") or "")[:10]
        if not day_text:
            continue
        bucket = days.setdefault(
            day_text,
            {
                "date": day_text,
                "points": 0,
                "temp_min": None,
                "temp_max": None,
                "wind_max_kmh": None,
                "pop_max": None,
                "conditions": [],
            },
        )
        bucket["points"] += 1
        bucket["temp_min"] = _min_value(bucket["temp_min"], point.get("temp"))
        bucket["temp_max"] = _max_value(bucket["temp_max"], point.get("temp"))
        bucket["wind_max_kmh"] = _max_value(bucket["wind_max_kmh"], point.get("wind_speed_kmh"))
        bucket["pop_max"] = _max_value(bucket["pop_max"], point.get("pop"))
        condition = point.get("condition")
        if condition and condition not in bucket["conditions"]:
            bucket["conditions"].append(condition)

    summary = [days[day_text] for day_text in sorted(days)]
    daylight = daylight_by_day(payload, source=source)
    for bucket in summary:
        sun = daylight.get(bucket["date"])
        if sun is not None:
            bucket["sunrise"] = sun.sunrise_text
            bucket["sunset"] = sun.sunset_text
            bucket["sun_source"] = sun.source_text
    return summary


# ---------------------------------------------------------------------------
# Восход и закат: данные WeatherAPI (astro) или офлайн-расчёт sun_times.py
# ---------------------------------------------------------------------------

def _days_of(points: Sequence[Mapping[str, Any]]) -> set[date]:
    """Даты, по которым есть точки прогноза (нужно, когда ответ API передан не целиком)."""
    days: set[date] = set()
    for point in points:
        day = _point_local_date(point)
        if day is not None:
            days.add(day)
    return days


def daylight_from_astro(payload: Mapping[str, Any]) -> dict[str, Daylight]:
    """Восход и закат по суткам из полей astro ответа WeatherAPI.

    Время приходит строками ('05:12 AM' или '05:12'). Сутки, где времени нет или оно
    бессмысленно (полярные широты), пропускаются — для них применяется расчёт
    sun_times.py (source="auto") либо сутки остаются без солнца (source="api").
    """
    result: dict[str, Daylight] = {}
    for day in (payload.get("forecast") or {}).get("forecastday") or []:
        if not isinstance(day, Mapping):
            continue
        day_text = str(day.get("date") or "").strip()[:10]
        astro = day.get("astro") or {}
        rise = parse_clock(astro.get("sunrise"))
        down = parse_clock(astro.get("sunset"))
        if not day_text or rise is None or down is None or rise >= down:
            continue
        result[day_text] = Daylight(day_text, rise, down, source="astro")
    return result


def _nearest_day(known: Mapping[str, Daylight], day: date) -> str | None:
    """Ближайшая дата, для которой восход и закат известны (None, если данных нет)."""
    best: str | None = None
    best_distance: int | None = None
    for day_text in known:
        try:
            candidate = date.fromisoformat(day_text)
        except ValueError:
            continue
        distance = abs((candidate - day).days)
        if best_distance is None or distance < best_distance:
            best, best_distance = day_text, distance
    return best


def _borrowed(daylight: Daylight, day: date) -> Daylight:
    """Переносит известные восход и закат на другие сутки (с пометкой, откуда они)."""
    return replace(
        daylight,
        day=day.isoformat(),
        borrowed_from=daylight.borrowed_from or daylight.day,
    )


def _calculated_days(days: Sequence[date], payload: Mapping[str, Any]) -> dict[str, Daylight]:
    """Запасной вариант: восход и закат считает sun_times.py по координатам и поясу города."""
    location = city_location(payload)
    tz_hours = city_tz_hours(payload)
    if location is None or tz_hours is None:
        return {}
    lat, lon = location
    return sun_times_by_day(days, lat, lon, tz_hours)


def daylight_by_day(
    payload: Mapping[str, Any],
    points: Sequence[Mapping[str, Any]] | None = None,
    *,
    source: str = "auto",
) -> dict[str, Daylight]:
    """Восход и закат каждых суток прогноза: {ISO-дата: Daylight}.

    Значения берутся из полей astro ответа WeatherAPI (forecast.forecastday[].astro) —
    для каждых суток прогноза отдельно. Если нужных суток в ответе нет, переносятся
    значения ближайших: это видно как Daylight.borrowed_from, а в сообщении — как
    «данные за ДД.ММ». Сутки без пригодных значений (в том числе полярные широты)
    считаются офлайн-расчётом sun_times.py, но только при source="auto" или "calc";
    при source="api" такие сутки остаются без солнца — вердикт по ним считается по
    резервному интервалу часов.
    """
    if source not in SUN_SOURCES:
        raise WeatherApiError(
            f"Неизвестный источник солнца {source!r}: ожидается {' / '.join(SUN_SOURCES)}."
        )
    days = sorted(set(group_points_by_day(payload)) or _days_of(points or ()))
    if not days:
        return {}
    known: dict[str, Daylight] = {} if source == "calc" else daylight_from_astro(payload)
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
# Публичные инструменты
# ---------------------------------------------------------------------------

def get_weather_forecast(
    city: str | None = None,
    *,
    lat: float | None = None,
    lon: float | None = None,
    api_key: str | None = None,
    lang: str = DEFAULT_LANG,
    days: int = DEFAULT_DAYS,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Основной инструмент: почасовой прогноз города списком плоских точек.

    Возвращает до 24 точек на сутки (шаг 1 час; бесплатный тариф — 3 суток, 72 точки)
    в порядке возрастания времени. Поля точки: date (UTC), date_local (время города),
    temp (°C), wind_speed (м/с), wind_speed_kmh (км/ч), condition, pop (0..1).
    """
    payload = fetch_forecast(
        city,
        lat=lat,
        lon=lon,
        api_key=api_key,
        lang=lang,
        days=days,
        timeout=timeout,
    )
    return map_forecast(payload)


def get_tomorrow_forecast(
    city: str | None = None,
    *,
    lat: float | None = None,
    lon: float | None = None,
    api_key: str | None = None,
    lang: str = DEFAULT_LANG,
    days: int = DEFAULT_DAYS,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Прогноз на завтра: 24 почасовые точки в местном времени города."""
    payload = fetch_forecast(
        city,
        lat=lat,
        lon=lon,
        api_key=api_key,
        lang=lang,
        days=days,
        timeout=timeout,
    )
    return tomorrow_points(payload)


def sun_report(daylight: Mapping[str, Daylight]) -> list[dict[str, Any]]:
    """Таблица восхода и заката для вывода: список словарей по суткам."""
    report: list[dict[str, Any]] = []
    for day_text in sorted(daylight):
        sun = daylight[day_text]
        item = {
            "date": day_text,
            "sunrise": sun.sunrise_text,
            "sunset": sun.sunset_text,
            "source": sun.source_text,
        }
        if sun.borrowed_from:
            item["borrowed_from"] = sun.borrowed_from
        if sun.polar:
            item["polar"] = sun.polar
        report.append(item)
    return report


# ---------------------------------------------------------------------------
# Командная строка
# ---------------------------------------------------------------------------

def _write_json(path: str, data: Any) -> None:
    """Пишет JSON в файл в UTF-8 (кириллица как есть, удобно читать глазами)."""
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


def _configure_stdout() -> None:
    """UTF-8 в stdout/stderr, чтобы русский текст не ломался в консоли."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def build_parser() -> argparse.ArgumentParser:
    """Описывает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        prog="get_weather_forecast.py",
        description=(
            "Почасовой прогноз WeatherAPI.com (шаг 1 час, бесплатный тариф — 3 суток) "
            "в плоской структуре date / date_local / temp / wind_speed / condition / pop."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--city", default=DEFAULT_CITY, help="Город прогноза (параметр q)")
    parser.add_argument("--lat", type=float, default=None, help="Широта (вместе с --lon вместо --city)")
    parser.add_argument("--lon", type=float, default=None, help="Долгота (вместе с --lat вместо --city)")
    parser.add_argument(
        "--api-key",
        dest="api_key",
        default=None,
        help="Ключ WeatherAPI.com (приоритетнее переменной окружения WEATHER_API_KEY)",
    )
    parser.add_argument("--lang", default=DEFAULT_LANG, help="Язык описаний погоды (lang=ru — русский)")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Сколько суток прогноза запросить (1…{MAX_DAYS}; бесплатный тариф — 3)",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Таймаут запросов, секунды")
    parser.add_argument(
        "--tomorrow",
        action="store_true",
        help="Вывести только почасовые точки на завтра (24 часа)",
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="Вывести агрегированный прогноз по суткам (температура, ветер, осадки, светлое время)",
    )
    parser.add_argument(
        "--sun",
        action="store_true",
        help="Вывести восход и закат по суткам прогноза (поля astro WeatherAPI)",
    )
    parser.add_argument(
        "--sun-source",
        dest="sun_source",
        choices=SUN_SOURCES,
        default="auto",
        help=(
            "Откуда брать восход и закат: auto — поля astro WeatherAPI, а если их нет "
            "— офлайн-расчёт sun_times.py; api — только данные API; calc — только расчёт"
        ),
    )
    parser.add_argument(
        "--from-file",
        dest="from_file",
        default=None,
        help="Читать сохранённый ответ API из файла вместо запроса к сети",
    )
    parser.add_argument(
        "--save-raw",
        dest="save_raw",
        default=None,
        help="Сохранить сырой ответ API в файл (для офлайн-разбора через --from-file)",
    )
    parser.add_argument("--out", default=None, help="Файл для результата (по умолчанию — stdout)")
    parser.add_argument("--indent", type=int, default=2, help="Отступ в JSON (0 — без отступов)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа CLI. Код выхода: 0 — успех, 1 — ошибка."""
    _configure_stdout()
    args = build_parser().parse_args(argv)

    try:
        if args.from_file:
            payload = load_forecast_file(args.from_file)
            print(f"Данные взяты из файла: {args.from_file}", file=sys.stderr)
        else:
            payload = fetch_forecast(
                args.city,
                lat=args.lat,
                lon=args.lon,
                api_key=args.api_key,
                lang=args.lang,
                days=args.days,
                timeout=args.timeout,
            )
        if args.save_raw:
            _write_json(args.save_raw, payload)
            print(f"Сырой ответ API сохранён: {args.save_raw}", file=sys.stderr)

        if args.sun:
            sun = daylight_by_day(payload, source=args.sun_source)
            result: Any = sun_report(sun)
            if not sun:
                print(
                    "Предупреждение: восход и закат определить не удалось "
                    "(нет полей astro в ответе API и нет данных для расчёта).",
                    file=sys.stderr,
                )
        elif args.tomorrow:
            result = tomorrow_points(payload)
            if not result:
                print(
                    "Предупреждение: в ответе API нет точек прогноза на завтра. "
                    "Попробуйте увеличить --days.",
                    file=sys.stderr,
                )
        elif args.daily:
            result = daily_summary(payload, source=args.sun_source)
        else:
            result = map_forecast(payload)
    except WeatherApiError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Ошибка файла: {exc}", file=sys.stderr)
        return 1

    indent = args.indent if args.indent > 0 else None
    text = json.dumps(result, ensure_ascii=False, indent=indent)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as file:
                file.write(text + "\n")
        except OSError as exc:
            print(f"Ошибка файла: {exc}", file=sys.stderr)
            return 1
        print(f"Результат записан: {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
