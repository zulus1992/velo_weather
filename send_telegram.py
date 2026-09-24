# -*- coding: utf-8 -*-
"""Вечерняя отправка прогноза на завтра в Telegram.

Что делает: берёт прогноз OpenWeatherMap (get_weather_forecast.py), сверяет его
с правилами weather_rules.xlsx (cycling_rules.py) и отправляет короткое сообщение
в Telegram через Bot API. Вердикт и «окна» считаются только по светлому времени
суток: кататься раньше восхода и после заката нельзя. Восход и закат считает
sun_times.py по координатам города из ответа API.

Пароль (доступ чата): бот ничего не публикует, пока в чате не отправят команду
    /password ПАРОЛЬ
(в группе — /password@имя_бота ПАРОЛЬ, подробности в README). Пароль задаётся
аргументом --password, переменной окружения WEATHER_BOT_PASSWORD или ключом
"password" в telegram_config.json. Удачный ввод команды запоминается в
chat_auth.json, поэтому дальше пароль присылать не нужно; сменить пароль или
забыть доступы — --reset-auth.

Настройка (один раз):
    1. В Telegram у @BotFather создать бота: /newbot -> получить токен (123456:AA...).
    2. Написать своему боту любое сообщение (бот не может писать первым).
    3. Узнать chat_id: открыть https://api.telegram.org/bot<ТОКЕН>/getUpdates
       и взять значение result[0].message.chat.id.
    4. Скопировать telegram_config.example.json в telegram_config.json и заполнить
       bot_token, chat_id и password. Как вариант — задать переменные окружения
       TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WEATHER_BOT_PASSWORD.
    5. Отправить в чат /password ПАРОЛЬ — до этого прогноз не публикуется.

Запуск (бот публикует прогноз на завтра в 18:00 по Минску — см. .github/workflows/weather.yml):
    python send_telegram.py --dry-run            # показать текст, ничего не отправлять
    python send_telegram.py                      # отправить вердикт на завтра (нужен пароль)
    python send_telegram.py --day weekend --show-rules
    python send_telegram.py --no-sun             # вердикт без ограничения восходом/закатом
    python send_telegram.py --reset-auth         # забыть доступы: снова требовать /password
    python send_telegram.py --day tomorrow --log morning_weather.log   # для планировщика
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import requests

from cycling_rules import (
    DEFAULT_HOUR_FROM,
    DEFAULT_HOUR_TO,
    RULES_FILE,
    RulesError,
    evaluate_days,
    format_report,
    read_rules,
    select_days,
)
from get_weather_forecast import (
    DEFAULT_CITY,
    DEFAULT_TIMEOUT,
    WeatherApiError,
    daylight_by_day,
    fetch_forecast,
    map_forecast,
)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
CONFIG_NAME = "telegram_config.json"
AUTH_NAME = "chat_auth.json"
DAY_LABELS = {"today": "сегодня", "tomorrow": "завтра", "weekend": "выходные"}
PASSWORD_COMMANDS = ("password", "пароль")   # /password ПАРОЛЬ открывает доступ чату
ACCESS_GRANTED = (
    "✅ Пароль принят. Прогноз погоды для велосипеда будет публиковаться в этом чате."
)


class TelegramError(RuntimeError):
    """Ошибка настройки или отправки сообщения в Telegram."""


def _clean(value: Any) -> str:
    """Убирает пробелы и лишние кавычки — частая ошибка при вставке токена и chat_id."""
    return str(value or "").strip().strip("'\"").strip()


def _read_config(path: Path) -> dict[str, Any]:
    """Читает telegram_config.json: пустой словарь, если файла нет."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TelegramError(f"Не удалось прочитать {path}: {exc}") from exc
    return dict(data) if isinstance(data, dict) else {}


def load_config(args: argparse.Namespace) -> tuple[str, str]:
    """Возвращает (bot_token, chat_id): аргументы -> окружение -> telegram_config.json."""
    token = _clean(args.token or os.getenv("TELEGRAM_BOT_TOKEN"))
    chat_id = _clean(args.chat_id or os.getenv("TELEGRAM_CHAT_ID"))
    config_path = Path(args.config)
    if not token or not chat_id:
        data = _read_config(config_path)
        token = token or _clean(data.get("bot_token"))
        chat_id = chat_id or _clean(data.get("chat_id"))
    if not token or not chat_id:
        raise TelegramError(
            "Не заданы bot_token и chat_id: заполните telegram_config.json "
            f"({config_path}) или задайте TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID."
        )
    if "@" in chat_id or not chat_id.lstrip("-").isdigit():
        raise TelegramError(
            f"chat_id должен быть числом (получено {chat_id!r}). Возьмите id из "
            "https://api.telegram.org/bot<ТОКЕН>/getUpdates -> message.chat.id "
            "или запустите: python send_telegram.py --find-chat-id"
        )
    return token, chat_id


# ---------------------------------------------------------------------------
# Пароль и доступ чата
# ---------------------------------------------------------------------------

def auth_path(args: argparse.Namespace) -> Path:
    """Путь к файлу состояния доступа: --auth-file или chat_auth.json рядом со скриптом."""
    return Path(args.auth_file) if args.auth_file else Path(__file__).with_name(AUTH_NAME)


def load_password(args: argparse.Namespace) -> str:
    """Пароль доступа: --password -> WEATHER_BOT_PASSWORD -> "password" в telegram_config.json."""
    password = _clean(args.password or os.getenv("WEATHER_BOT_PASSWORD"))
    if password:
        return password
    return _clean(_read_config(Path(args.config)).get("password"))


def _password_hash(password: str) -> str:
    """SHA-256 пароля: в файле состояния сам пароль не хранится."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _same_password(left: str, right: str) -> bool:
    """Сравнивает пароли постоянным по времени способом."""
    return hmac.compare_digest(_password_hash(left), _password_hash(right))


def load_auth(path: Path) -> dict[str, Any]:
    """Читает состояние доступов чатов; битый файл считается пустым состоянием."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Предупреждение: не удалось прочитать {path}: {exc}", file=sys.stderr)
        return {}
    return dict(data) if isinstance(data, dict) else {}


def save_auth(path: Path, chat_id: str, password: str) -> None:
    """Запоминает, что чат открыл доступ паролем (сохраняется только хеш пароля)."""
    data = load_auth(path)
    data[str(chat_id)] = {
        "password_sha256": _password_hash(password),
        "authorized_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise TelegramError(f"Не удалось сохранить {path}: {exc}") from exc


def reset_auth(path: Path) -> bool:
    """Забывает доступы чатов: публикация снова потребует /password ПАРОЛЬ."""
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError as exc:
        raise TelegramError(f"Не удалось удалить {path}: {exc}") from exc
    return True


def is_authorized(path: Path, chat_id: str, password: str) -> bool:
    """Проверяет сохранённый доступ: чат когда-то открыл его этим паролем."""
    saved = (load_auth(path).get(str(chat_id)) or {}).get("password_sha256")
    return isinstance(saved, str) and hmac.compare_digest(saved, _password_hash(password))


def command_password(text: Any) -> str | None:
    """Пароль из команды доступа: '/password ПАРОЛЬ' -> 'ПАРОЛЬ', иначе None."""
    parts = str(text or "").strip().split(maxsplit=1)
    if not parts:
        return None
    command = parts[0].split("@")[0].lstrip("/").lower()
    if command not in PASSWORD_COMMANDS:
        return None
    return parts[1].strip() if len(parts) > 1 else ""


def chat_password_from_updates(
    updates: Sequence[dict[str, Any]],
    chat_id: str,
) -> str | None:
    """Ищет в свежих сообщениях команду /password из нужного чата и возвращает пароль."""
    for update in updates:
        for key in ("message", "edited_message", "channel_post"):
            message = update.get(key) or {}
            if str((message.get("chat") or {}).get("id")) != str(chat_id):
                continue
            password = command_password(message.get("text"))
            if password is not None:
                return password
    return None


def check_access(args: argparse.Namespace, token: str, chat_id: str) -> str:
    """Проверяет доступ чата к публикации: 'state' или 'command' (иначе TelegramError).

    Пароль обязателен: без него бот ничего не публикует. Доступ открывает команда
    /password ПАРОЛЬ в самом чате — она ищется среди свежих сообщений бота (getUpdates
    отдаёт последние ~24 часа) и запоминается в chat_auth.json, чтобы следующие
    запуски эту команду уже не требовали.
    """
    password = load_password(args)
    if not password:
        raise TelegramError(
            "Не задан пароль бота: заполните \"password\" в telegram_config.json, задайте "
            "WEATHER_BOT_PASSWORD или --password. Без пароля бот ничего не публикует."
        )

    state_path = auth_path(args)
    if is_authorized(state_path, chat_id, password):
        return "state"

    sent = chat_password_from_updates(get_updates(token, timeout=args.timeout), chat_id)
    if sent is not None:
        if _same_password(sent, password):
            save_auth(state_path, chat_id, password)
            return "command"
        raise TelegramError(
            "Пароль в команде /password не совпадает с настроенным: публикация запрещена."
        )

    raise TelegramError(
        f"Чат {chat_id} не авторизован: отправьте в чат команду «/password ПАРОЛЬ» и повторите "
        "запуск. Без пароля бот ничего не публикует. Если бот в группе, разрешите ему видеть "
        "сообщения (BotFather -> /setprivacy -> Disable) либо пишите /password@имя_бота ПАРОЛЬ."
    )


def send_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    silent: bool = False,
) -> None:
    """Отправляет текст в чат Telegram через Bot API."""
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if silent:
        payload["disable_notification"] = True

    try:
        response = requests.post(TELEGRAM_API.format(token=token), json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise TelegramError(f"Не удалось обратиться к Telegram: {exc}") from exc

    if response.status_code == 401:
        raise TelegramError("Telegram отклонил токен бота (HTTP 401): проверьте bot_token.")
    if response.status_code == 400:
        raise TelegramError(
            f"Telegram вернул HTTP 400 (обычно неверный chat_id): {response.text[:200]}"
        )
    if response.status_code != 200:
        raise TelegramError(f"Telegram вернул HTTP {response.status_code}: {response.text[:200]}")

    try:
        data = response.json()
    except ValueError as exc:
        raise TelegramError("Ответ Telegram не является корректным JSON.") from exc
    if not data.get("ok"):
        raise TelegramError(f"Telegram вернул ошибку: {data.get('description') or data}")


def get_updates(token: str, *, timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """Возвращает последние обновления бота (нужно для поиска chat_id)."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise TelegramError(f"Не удалось обратиться к Telegram: {exc}") from exc
    if response.status_code == 401:
        raise TelegramError("Telegram отклонил токен бота (HTTP 401): проверьте токен.")
    if response.status_code != 200:
        raise TelegramError(f"Telegram вернул HTTP {response.status_code}: {response.text[:200]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise TelegramError("Ответ Telegram не является корректным JSON.") from exc
    if not data.get("ok"):
        raise TelegramError(f"Telegram вернул ошибку: {data.get('description') or data}")
    return list(data.get("result") or [])


def get_me(token: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Возвращает данные бота по токену (проверяет, что токен верный и чей он)."""
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise TelegramError(f"Не удалось обратиться к Telegram: {exc}") from exc
    if response.status_code == 401:
        raise TelegramError("Telegram отклонил токен бота (HTTP 401): проверьте токен.")
    if response.status_code != 200:
        raise TelegramError(f"Telegram вернул HTTP {response.status_code}: {response.text[:200]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise TelegramError("Ответ Telegram не является корректным JSON.") from exc
    if not data.get("ok"):
        raise TelegramError(f"Telegram вернул ошибку: {data.get('description') or data}")
    return dict(data.get("result") or {})


def print_chat_ids(token: str, *, timeout: float = DEFAULT_TIMEOUT) -> int:
    """Печатает имя бота и доступные ему chat_id (режим --find-chat-id)."""
    token = _clean(token)
    if not token:
        print("Нужен --token или переменная окружения TELEGRAM_BOT_TOKEN.", file=sys.stderr)
        return 1
    try:
        me = get_me(token, timeout=timeout)
    except TelegramError as exc:
        print(f"Ошибка токена: {exc}", file=sys.stderr)
        return 1
    print(f"Бот: @{me.get('username')} (id {me.get('id')})")

    try:
        updates = get_updates(token, timeout=timeout)
    except TelegramError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    chats: dict[str, str] = {}
    for update in updates:
        for key in ("message", "edited_message", "channel_post", "my_chat_member", "callback_query"):
            chat = (update.get(key) or {}).get("chat") or {}
            if chat.get("id") is None:
                continue
            name = (
                chat.get("title")
                or " ".join(part for part in (chat.get("first_name"), chat.get("last_name")) if part)
                or chat.get("username")
                or ""
            )
            chats[str(chat["id"])] = name

    if not chats:
        print(
            "Сообщений не найдено: откройте Telegram, напишите своему боту любое сообщение "
            "(например, /start) и повторите команду. Бот не может писать первым.",
            file=sys.stderr,
        )
        return 1
    for chat_id, name in chats.items():
        print(f"chat_id: {chat_id}  ({name})")
    return 0


def parse_hours(text: str) -> tuple[int, int]:
    """Разбирает интервал часов вида '7-22' в пару (с какого часа, по какой не включая)."""
    parts = str(text).replace(":", "-").split("-")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("ожидается интервал вида 7-22")
    try:
        hour_from, hour_to = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("часы должны быть целыми числами") from exc
    if not 0 <= hour_from < hour_to <= 24:
        raise argparse.ArgumentTypeError("нужно 0 <= час_от < час_до <= 24")
    return hour_from, hour_to


def build_message(args: argparse.Namespace) -> str:
    """Формирует текст сообщения: прогноз OpenWeatherMap + правила -> вердикт по дням.

    Кататься можно только между восходом и закатом, поэтому точки прогноза
    ограничиваются светлым временем суток (для дней, где посчитаны солнце).
    """
    rules = read_rules(args.rules_file)
    payload = fetch_forecast(args.city, lat=args.lat, lon=args.lon, timeout=args.timeout)
    points = map_forecast(payload)
    sun = {} if args.no_sun else daylight_by_day(payload, points)
    results = evaluate_days(
        points, rules, hour_from=args.hours[0], hour_to=args.hours[1], sun=sun
    )
    selected = select_days(results, args.day)
    if not selected:
        available = ", ".join(result.day for result in results)
        raise TelegramError(
            f"В прогнозе нет дня для режима «{args.day}» (доступные даты: {available})."
        )
    label = DAY_LABELS.get(args.day, "")
    title = f"🚴 Погода для велосипеда: {args.city}" + (f" — {label}" if label else "")
    return format_report(selected, title=title, rules=rules if args.show_rules else ())


def _append_log(path: str, text: str) -> None:
    """Дописывает строку в лог-файл (нужно при запуске планировщиком задач)."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a", encoding="utf-8") as file:
        file.write(f"[{stamp}] {text}\n")


def build_parser() -> argparse.ArgumentParser:
    """Описывает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        prog="send_telegram.py",
        description=(
            "Отправляет в Telegram вердикт «можно ли кататься»: прогноз OpenWeatherMap "
            "сверяется с правилами weather_rules.xlsx."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--day",
        default="tomorrow",
        choices=("today", "tomorrow", "weekend", "all"),
        help="Какой день описывать",
    )
    parser.add_argument("--city", default=DEFAULT_CITY, help="Город прогноза")
    parser.add_argument("--lat", type=float, default=None, help="Широта (вместе с --lon вместо --city)")
    parser.add_argument("--lon", type=float, default=None, help="Долгота (вместе с --lat вместо --city)")
    parser.add_argument(
        "--rules-file",
        dest="rules_file",
        default=str(RULES_FILE),
        help="Путь к weather_rules.xlsx",
    )
    parser.add_argument(
        "--show-rules",
        dest="show_rules",
        action="store_true",
        help="Добавить в сообщение таблицу правил",
    )
    parser.add_argument(
        "--hours",
        type=parse_hours,
        default=f"{DEFAULT_HOUR_FROM}-{DEFAULT_HOUR_TO}",
        help=(
            "Резервный интервал часов, если восход и закат неизвестны: "
            "ЧАС_ОТ-ЧАС_ДО (например, 7-22, 0-24 для круглосуточно)"
        ),
    )
    parser.add_argument(
        "--no-sun",
        dest="no_sun",
        action="store_true",
        help="Не ограничивать катание восходом и закатом: вердикт по --hours",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Токен бота (иначе TELEGRAM_BOT_TOKEN или telegram_config.json)",
    )
    parser.add_argument(
        "--chat-id",
        dest="chat_id",
        default=None,
        help="ID чата (иначе TELEGRAM_CHAT_ID или telegram_config.json)",
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).with_name(CONFIG_NAME)),
        help="Файл с bot_token, chat_id и password",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Пароль доступа к публикации (иначе WEATHER_BOT_PASSWORD или \"password\" в конфиге)",
    )
    parser.add_argument(
        "--auth-file",
        dest="auth_file",
        default=None,
        help="Файл состояния доступов чатов (по умолчанию chat_auth.json рядом со скриптом)",
    )
    parser.add_argument(
        "--reset-auth",
        dest="reset_auth",
        action="store_true",
        help="Забыть доступы чатов: публикация снова потребует /password ПАРОЛЬ",
    )
    parser.add_argument(
        "--find-chat-id",
        dest="find_chat_id",
        action="store_true",
        help="Показать chat_id по последним сообщениям боту и выйти (нужен --token)",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Показать сообщение, ничего не отправлять",
    )
    parser.add_argument("--silent", action="store_true", help="Отправить без звукового уведомления")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Таймаут запросов, секунды")
    parser.add_argument("--log", default=None, help="Дописывать результат работы в лог-файл")
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


def _log(args: argparse.Namespace, message: str) -> None:
    """Пишет сообщение в лог-файл, если он задан."""
    if not args.log:
        return
    try:
        _append_log(args.log, message)
    except OSError as exc:
        print(f"Не удалось записать лог {args.log}: {exc}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа CLI. Код выхода: 0 — успех, 1 — ошибка."""
    _configure_stdout()
    args = build_parser().parse_args(argv)

    if args.find_chat_id:
        token = args.token or os.getenv("TELEGRAM_BOT_TOKEN") or ""
        return print_chat_ids(token, timeout=args.timeout)

    state_path = auth_path(args)
    if args.reset_auth:
        try:
            removed = reset_auth(state_path)
        except TelegramError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        print(f"Доступы чатов забыты: {state_path}" if removed else f"Доступов нет: {state_path}")
        return 0

    try:
        if args.dry_run:
            print(build_message(args))
            print("\n[dry-run] сообщение не отправлено", file=sys.stderr)
            return 0
        token, chat_id = load_config(args)
        access = check_access(args, token, chat_id)
        print(
            "Доступ чата: уже разрешён (chat_auth.json)"
            if access == "state"
            else "Доступ чата: разрешён командой /password"
        )
        text = build_message(args)
        if access == "command":
            send_message(token, chat_id, ACCESS_GRANTED, timeout=args.timeout)
        send_message(token, chat_id, text, timeout=args.timeout, silent=args.silent)
    except (WeatherApiError, RulesError, TelegramError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        _log(args, f"Ошибка: {exc}")
        return 1
    except OSError as exc:
        print(f"Ошибка файла: {exc}", file=sys.stderr)
        _log(args, f"Ошибка файла: {exc}")
        return 1

    print("Отправлено в Telegram.")
    print(text)
    _log(args, "Отправлено: " + text.replace("\n", " | "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())