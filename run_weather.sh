#!/usr/bin/env bash
# Отправка прогноза погоды для велосипеда в Telegram — вариант B (Termux на телефоне).
#
# Одно сообщение в день: прогноз на завтра вечером, как и в GitHub Actions
# (weather_schedule.json: слот «вечер», 18:00 по Минску, cron "0 15 * * *").
#
# Ручной запуск:
#     bash ~/weather/run_weather.sh now        # отправить прямо сейчас, не ожидая 18:00
#     bash ~/weather/run_weather.sh --dry-run  # только показать текст, ничего не отправлять
#
# Автоматический запуск (Android JobScheduler, задачу ставит android_termux_setup.sh):
# скрипт без аргументов работает в режиме «расписание» — отправляет сообщение один раз
# в день, только начиная с TARGET_HOUR (по умолчанию 18:00 по времени телефона) и только
# если сегодня ещё не отправлял (отметка — файл .last_send). Поэтому задачу можно
# просыпать каждый час: сообщение всё равно придёт одно за сутки.
#
# Настройки (можно менять переменными окружения):
#   TARGET_HOUR   с какого часа можно отправлять (по умолчанию 18)
#   DAY           период прогноза: today / tomorrow / weekend / all (по умолчанию tomorrow)
#   LOG_FILE      файл лога (по умолчанию weather.log рядом со скриптом)
#   PYTHON        интерпретатор (по умолчанию python)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TARGET_HOUR="${TARGET_HOUR:-18}"
DAY="${DAY:-tomorrow}"
LOG_FILE="${LOG_FILE:-$SCRIPT_DIR/weather.log}"
STATE_FILE="${STATE_FILE:-$SCRIPT_DIR/.last_send}"
PYTHON="${PYTHON:-python}"
CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/telegram_config.json}"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"; }

# Режим: "schedule" — автоматический (одно сообщение в день вечером),
# "now" — ручной: отправляем сразу, состояние «уже отправлено» не трогаем.
mode="schedule"
day_args=(--day "$DAY")
extra=()
args_list=("$@")
index=0
while [ "$index" -lt "${#args_list[@]}" ]; do
  arg="${args_list[$index]}"
  case "$arg" in
    now|--now) mode="now" ;;                      # отправить сразу, не ожидая TARGET_HOUR
    schedule) ;;                                  # явный автоматический режим
    --day)                                        # период задан вручную: --day tomorrow
      mode="now"
      day_args=()
      extra+=("--day")
      if [ $((index + 1)) -lt "${#args_list[@]}" ]; then
        index=$((index + 1))
        extra+=("${args_list[$index]}")
      fi
      ;;
    --day=*) mode="now"; day_args=(); extra+=("$arg") ;;
    --dry-run) mode="now"; extra+=("$arg") ;;
    "") ;;
    *) extra+=("$arg") ;;
  esac
  index=$((index + 1))
done

if [ "$mode" = "schedule" ]; then
  hour="$((10#$(date '+%H')))"
  today="$(date '+%Y-%m-%d')"
  if [ "$hour" -lt "$TARGET_HOUR" ]; then
    log "Пропуск: ещё не ${TARGET_HOUR}:00 (сейчас $(date '+%H:%M'))"
    exit 0
  fi
  if [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE")" = "$today" ]; then
    log "Пропуск: прогноз на сегодня ($today) уже отправлен"
    exit 0
  fi
fi

cmd=("$PYTHON" send_telegram.py)
if [ "${#day_args[@]}" -gt 0 ]; then
  cmd+=("${day_args[@]}")
fi
cmd+=(--config "$CONFIG_FILE" --log "$LOG_FILE")
if [ "${#extra[@]}" -gt 0 ]; then
  cmd+=("${extra[@]}")
fi

log "Запуск: ${cmd[*]}"

status=0
"${cmd[@]}" || status=$?

if [ "$status" -ne 0 ]; then
  log "Не получилось (код $status) — подробности ниже в этом же логе"
  exit "$status"
fi

# Отметку «сегодня уже отправлено» ставит только автоматический режим:
# при ручном запуске (now, --dry-run) поведение остаётся предсказуемым.
if [ "$mode" = "schedule" ]; then
  date '+%Y-%m-%d' >"$STATE_FILE"
  log "Готово: прогноз на $DAY отправлен"
fi
