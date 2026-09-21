# 🚴 Прогноз для велосипеда → Telegram (работает без ПК, с Android)

Скрипты берут прогноз OpenWeatherMap (5 суток, шаг 3 часа), сверяют его с правилами
`weather_rules.xlsx` (нужно / нормально / можно) и отправляют короткое сообщение в Telegram.

| Файл | Назначение |
|---|---|
| `get_weather_forecast.py` | прогноз OpenWeatherMap в плоском виде (`date`, `date_local`, `temp`, `wind_speed`, `wind_speed_kmh`, `condition`, `pop`) |
| `cycling_rules.py` | правила из xlsx + расчёт вердикта и «окон» времени |
| `send_telegram.py` | отправка в Telegram (`--day`, `--hours`, `--dry-run`, `--find-chat-id`, `--log`) |
| `weather_rules.xlsx` | сами правила (температура / ветер км/ч / вероятность осадков %) |
| `.github/workflows/weather.yml` | расписание в облаке GitHub — **вариант A** |
| `android_termux_setup.sh` | запуск и расписание на самом телефоне — **вариант B** |

## Шаг 0. Бот и chat_id (нужен только телефон)

1. Telegram → `@BotFather` → `/newbot` → придумать имя и username → получить токен вида `123456:AA...`.
2. Написать своему боту любое сообщение (бот не может писать первым).
3. Узнать `chat_id` прямо в браузере телефона:
   `https://api.telegram.org/bot<ТОКЕН>/getUpdates` → в ответе будет `"chat":{"id":987654321,...}`.

## Вариант A (рекомендую): GitHub Actions — ПК и телефон не нужны вообще

Работает на серверах GitHub по расписанию, даже если телефон выключен.

1. На github.com создать **приватный** репозиторий, например `weather`.
2. Загрузить в корень репозитория: `get_weather_forecast.py`, `cycling_rules.py`,
   `send_telegram.py`, `weather_rules.xlsx`, `requirements.txt`, `.gitignore`
   и создать файл по пути `.github/workflows/weather.yml`
   (Add file → Create new file → ввести путь целиком, затем вставить содержимое).
   С телефона это удобно делать в мобильном браузере (режим «Версия для ПК»).
3. Settings → Secrets and variables → Actions → **New repository secret**:
   * `TELEGRAM_BOT_TOKEN` — токен из BotFather,
   * `TELEGRAM_CHAT_ID` — ваш chat_id.
4. Вкладка **Actions** → «I understand my workflows, go ahead» → выбрать
   «Утренний прогноз для велосипеда» → **Run workflow** (ручная проверка, нажатие с телефона).
5. Через ~20 секунд придёт сообщение в Telegram; при ошибке подробности будут в логе шага.

Дальше всё автоматом: `cron: "0 5 * * *"` = 05:00 UTC = **08:00 по Минску** каждый день.
Поменять время или день прогноза — правится прямо в `.github/workflows/weather.yml` из телефона:
* время: `cron: "0 4 * * *"` (07:00 по Минску),
* период: `--day today` (по умолчанию), `--day tomorrow`, `--day weekend`, `--day all`,
* добавить `--show-rules`, чтобы в сообщении была таблица правил.

Полезно: `Actions → нужный запуск → Re-run jobs` — повторить отправку без ПК.
Секреты GitHub маскирует в логах, токен в открытом виде нигде не светится.

### Где живут секреты и как они подставляются

`${{ secrets.ИМЯ }}` понимает **только сам GitHub Actions и только в файлах workflow**
(`.github/workflows/*.yml`) — в `env:`, `with:` и `run:`. Внутри Python-файлов такая
запись не сработает: интерпретатор увидит обычную строку, и запрос уйдёт с мусорным ключом.

Схема:

* `send_telegram.py` читает доступы из переменных окружения:
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, а ключ погоды — `WEATHER_API_KEY`
  (читает его `get_weather_forecast.py`);
* workflow передаёт их в `env:` шага — так секретов в коде нет.

| Секрет GitHub | Что это | Нужен |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | токен бота из BotFather | обязательно |
| `TELEGRAM_CHAT_ID` | числовой id чата (у групп отрицательный) | обязательно |
| `WEATHER_API_KEY` | ключ OpenWeatherMap (погода) | обязательно для репозитория: `get_weather_forecast.py` берёт ключ из этой переменной, в коде ключа нет (старое имя `OPENWEATHER_API_KEY` тоже поддерживается) |

Чтобы ключ погоды не хранился в коде: создайте секрет `WEATHER_API_KEY`, а в
`get_weather_forecast.py` оставьте `DEFAULT_API_KEY = os.getenv("WEATHER_API_KEY", "")`.
Скрипт возьмёт ключ из окружения, а если его нет — скажет понятное «Не задан ключ
OpenWeatherMap: укажите --api-key или задайте переменную окружения WEATHER_API_KEY».

## Вариант B: на самом телефоне (Termux, без облака)

1. Поставить **Termux** и **Termux:API** из F-Droid (версия из Play Market не подходит).
2. Скопировать в `~/weather` файлы `get_weather_forecast.py`, `cycling_rules.py`,
   `send_telegram.py`, `weather_rules.xlsx`, `android_termux_setup.sh`
   (например, через Telegram «Избранное» → скачать → `mv /sdcard/Download/<файл> ~/weather/`).
3. Выполнить:
   ```bash
   cd ~/weather && bash android_termux_setup.sh
   ```
4. Скрипт сам: поставит Python и `requests`, создаст `telegram_config.json`
   (впишите токен и chat_id: `nano ~/weather/telegram_config.json`), отправит тестовое
   сообщение и зарегистрирует ежедневную задачу в Android JobScheduler.

Проверка и управление:
```bash
bash ~/weather/run_morning.sh            # отправить сейчас
termux-job-scheduler --pending           # список задач
termux-job-scheduler --cancel            # отменить задачу
tail -n 20 ~/weather/morning_weather.log # лог последнего запуска
```
Минус варианта: Android сам выбирает точное время фоновой задачи (задержка обычно
до 15–60 минут), а если телефон выключен — сообщение придёт позже. Плюс: данные не покидают телефон.

## Вариант C: PythonAnywhere (облако, одна ежедневная задача бесплатно)

1. Зарегистрироваться на pythonanywhere.com (Free).
2. Files → загрузить `get_weather_forecast.py`, `cycling_rules.py`, `send_telegram.py`,
   `weather_rules.xlsx`, `telegram_config.json` (токен и chat_id).
3. Bash console: `pip3 install --user requests`.
4. Tasks → задать время (по умолчанию там UTC, т.е. 05:00 = 08:00 по Минску) и команду:
   `python3.10 send_telegram.py --day today`
5. Нажать **Run now** для проверки.

## Параметры `send_telegram.py`

| Ключ | Значение |
|---|---|
| `--day today / tomorrow / weekend / all` | какой период описывать (по умолчанию `tomorrow`) |
| `--hours 7-22` | «светлое время» для вердикта (`0-24` — круглосуточно, тогда видны и ночные окна) |
| `--show-rules` | добавить в сообщение строки правил (`нужно / нормально / можно`) |
| `--city Minsk` (или `--lat/--lon`) | город прогноза |
| `--token`, `--chat-id`, `--config` | доступы: аргумент → переменные `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` → `telegram_config.json` |
| `--dry-run` | показать сообщение, ничего не отправлять (удобно проверять правки) |
| `--find-chat-id` | вывести chat_id по последним сообщениям боту |
| `--log FILE` | дописывать результат в лог-файл (нужно при запуске по расписанию) |
| `--silent` | отправить без звукового уведомления |

## Как считается вердикт

* Правила читаются из `weather_rules.xlsx`: `нужно` (t > 18 °C, ветер < 4 км/ч, осадки 0 %),
  `нормально` (t > 12, ветер < 10, осадки < 10 %), `можно` (t > 5, ветер < 14, осадки < 20 %).
  Проверяется от лучшей строки к худшей — побеждает первая подошедшая.
* Оценка идёт по светлому времени (07:00–22:00 по времени города), поэтому дождливая ночь
  не превращает день в «можно».
* `pop` из OpenWeatherMap — вероятность осадков 0…1, в правилах сравнивается как проценты.
* Ветер в API — м/с, в правилах — км/ч, поэтому в сообщении используется `wind_speed_kmh`.

## Частые вопросы и ошибки

| Симптом | Что делать |
|---|---|
| Сообщение не приходит | Проверить, что написали боту первым; проверить токен и chat_id; смотреть лог запуска (Actions / `morning_weather.log`) |
| `HTTP 400 ... chat not found` | Неверный `chat_id` или боту не писали: Telegram → напишите боту `/start` → взять id из `.../getUpdates` → **перезаписать** секрет `TELEGRAM_CHAT_ID` (секреты GitHub не показываются, только перезапись) |
| `401` | Токен отозван или скопирован с ошибкой — перевыпустить в BotFather |
| `Город не найден (HTTP 404)` | Проверить `--city` (например, `Minsk,BY`) |
| Прогноз «пустой» для воскресенья | Бесплатный API даёт только 5 суток — данные появятся позже |
| Расписание в GitHub сработало с задержкой 10–30 минут | Норма для GitHub Actions; для точного времени используйте вариант B/C |

### Если в Actions появилось `Bad Request: chat not found`

1. Откройте Telegram и напишите своему боту `/start` — бот не может писать первым, без этого чата для него «не существует».
2. Запустите workflow: в логе шага «Диагностика: имя бота и доступные chat_id» появится
   `Бот: @ваш_бот (id ...)` и строки вида `chat_id: 987654321  (Иван)`.
3. Settings → Secrets and variables → Actions → `TELEGRAM_CHAT_ID` → **Update** — вставить id
   **числом** (без кавычек, без `@username`, без пробелов; для группы/канала id отрицательный).
4. Снова **Run workflow** — сообщение должно прийти.

Скрипт сам подчищает пробелы и кавычки вокруг токена/chat_id и заранее сообщает, если вместо id
передан `@username`.

## Локальный запуск (по желанию, на ПК)

```powershell
python send_telegram.py --dry-run                 # проверить текст
python send_telegram.py --day tomorrow            # отправить
python get_weather_forecast.py --tomorrow          # только данные прогноза
```

