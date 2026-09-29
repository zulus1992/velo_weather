#!/usr/bin/env bash
# Настройка варианта B: прогноз для велосипеда прямо на телефоне (Termux), без облака.
#
# Запуск:  cd ~/weather && bash android_termux_setup.sh
#
# Что делает скрипт:
#   1) проверяет, что рядом лежат файлы проекта;
#   2) ставит в Termux python и termux-api (плюс нужен пакет Termux:API из F-Droid);
#   3) ставит зависимость requests (requirements.txt);
#   4) создаёт telegram_config.json из примера — токен, chat_id, пароль, ключ WeatherAPI.com;
#   5) показывает тестовый прогноз: send_telegram.py --dry-run (ничего не отправляет);
#   6) ставит ежедневную задачу в Android JobScheduler. Отправляет run_weather.sh, который
#      сам следит за временем: одно сообщение в день начиная с TARGET_HOUR (по умолчанию
#      09:00 — как слот «утро» в weather_schedule.json и cron "25 6 * * *" в Actions).
#
# Настройки (переменные окружения): JOB_ID, JOB_PERIOD_MS, TARGET_HOUR.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

JOB_ID="${JOB_ID:-1}"
JOB_PERIOD_MS="${JOB_PERIOD_MS:-3600000}"   # задача просыпается раз в час, отправка — одна за сутки
TARGET_HOUR="${TARGET_HOUR:-9}"
CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/telegram_config.json}"

say() { printf '%s\n' "$*"; }

# 1. Файлы проекта на месте?
missing=()
for file in get_weather_forecast.py cycling_rules.py sun_times.py send_telegram.py \
            weather_rules.xlsx telegram_config.example.json run_weather.sh; do
  [ -f "$SCRIPT_DIR/$file" ] || missing+=("$file")
done
if [ "${#missing[@]}" -gt 0 ]; then
  say "Не хватает файлов: ${missing[*]}"
  say "Скопируйте их в $SCRIPT_DIR (см. README, «Вариант B»)."
  exit 1
fi

# 2. Termux и нужные пакеты
if ! command -v pkg >/dev/null 2>&1; then
  say "Этот скрипт рассчитан на Termux (Android, F-Droid). На ПК используйте вариант A/C."
  exit 1
fi
say "Обновляю списки пакетов и ставлю python + termux-api..."
pkg update -y >/dev/null 2>&1 || true
pkg install -y python termux-api

# 3. Зависимости Python
say "Ставлю зависимости Python..."
python -m pip install --upgrade pip >/dev/null 2>&1 || true
if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
  python -m pip install -r "$SCRIPT_DIR/requirements.txt"
else
  python -m pip install requests
fi

# 4. Конфиг с доступами
if [ -f "$CONFIG_FILE" ]; then
  say "Конфиг уже есть: $CONFIG_FILE"
else
  cp "$SCRIPT_DIR/telegram_config.example.json" "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE"
  say "Создал $CONFIG_FILE — впишите туда токен бота, chat_id, пароль и ключ WeatherAPI.com:"
  say "    nano $CONFIG_FILE"
fi
chmod +x "$SCRIPT_DIR/run_weather.sh" "$SCRIPT_DIR/android_termux_setup.sh" 2>/dev/null || true

# 5. Проверка без отправки (пароль и сеть для этого не нужны)
say "Проверяю прогноз без отправки (send_telegram.py --dry-run)..."
python "$SCRIPT_DIR/send_telegram.py" --day tomorrow --config "$CONFIG_FILE" --dry-run \
  || say "Проверка не прошла — смотрите текст ошибки выше и заполненность конфига."

# 6. Ежедневная задача Android JobScheduler
JOB_CMD=(termux-job-scheduler --script "$SCRIPT_DIR/run_weather.sh" --job-id "$JOB_ID"
         --period-ms "$JOB_PERIOD_MS" --network any --battery-not-low false --persisted true)
if command -v termux-job-scheduler >/dev/null 2>&1; then
  say "Ставлю задачу: одно сообщение в день, начиная с ${TARGET_HOUR}:00..."
  if "${JOB_CMD[@]}"; then
    say "Задача поставлена (id $JOB_ID)."
  else
    say "Задачу поставить не получилось. Повторите вручную:"
    say "    ${JOB_CMD[*]}"
  fi
else
  say "Команда termux-job-scheduler не найдена: поставьте приложение Termux:API из F-Droid,"
  say "затем выполните:"
  say "    ${JOB_CMD[*]}"
fi

# 7. Что делать дальше
say ""
say "Дальше:"
say "  1) отправьте в чат с ботом команду:  /password ВАШ_ПАРОЛЬ"
say "  2) проверьте отправку сейчас:        bash $SCRIPT_DIR/run_weather.sh now"
say "  3) список задач:                     termux-job-scheduler --pending"
say "     отменить задачу:                  termux-job-scheduler --cancel"
say "  4) лог последних запусков:           tail -n 20 $SCRIPT_DIR/weather.log"
say ""
say "Время отправки задаёт TARGET_HOUR=${TARGET_HOUR} (в run_weather.sh) — оно повторяет"
say "weather_schedule.json: одно сообщение в день, утром, прогноз на завтра."
