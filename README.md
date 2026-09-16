# OsuTourneyBot

See [CHANGELOG.md](CHANGELOG.md) for the English release notes.

Discord-бот для управления пулами карт osu! и проведения тестовых матчей через Bancho IRC. Проект находится в активной разработке и сейчас используется для проверки ladder-инфраструктуры без рейтинговой формулы.

## Что уже работает

- MongoDB как основное хранилище пулов, карт, истории модерации и матчей.
- Загрузка и обновление osu! API OAuth-токена при старте и в фоне.
- Пулы для STD, Taiko, CTB и Mania 4K/7K.
- Кэширование распарсенных данных карт в MongoDB.
- Slash-команды для создания, просмотра, редактирования, удаления и отправки пулов на модерацию.
- Статусы пулов: `Draft`, `Pending`, `Ranked`, `Unranked`.
- Persistent-кнопки `Rank`/`Unrank` в чате модераторов с восстановлением после перезапуска.
- Bancho IRC: создание MP-комнаты, приглашения игроков, ожидание подключения, ролл, pick/ban и таймеры.
- Автоматическое восстановление Bancho IRC после краткого разрыва сети/VPN: повторный вход в активные MP-комнаты и восстановление стадии матча.
- `/osu-connect`: одноразовая привязка Discord-пользователя к osu! аккаунту через код в osu! PM.
- Установка выбранной карты и модов через `!mp map` и `!mp mods`.
- Проверка `!mp settings` после каждого `All players are ready` и перед запуском карты, включая карту, игроков, готовность и NoFail для FreeMod.
- Сохранение счёта серии и результатов сыгранных карт.

Текущая матч-система работает без рейтинга. Glicko-2 и ladder-рейтинги будут добавлены отдельным этапом.

## Структура

```text
bot.py                 запуск Discord-бота
database.py            MongoDB persistence
osu_api.py             osu! API и кэш данных карт
bancho_irc.py          IRC-клиент Bancho
utils.py               парсинг и форматирование пулов
launcher.ps1           визуальный Windows-лаунчер
start_launcher.vbs     запуск лаунчера двойным кликом без консоли
autostart_launcher.vbs запуск лаунчера с автоматическим стартом бота
install_autostart.ps1  включение запуска при входе в Windows
remove_autostart.ps1   отключение запуска при входе в Windows
cogs/                  Discord-команды
rulesets/              правила категорий для режимов
docs/                  документы проекта
tests/                 автоматические тесты правил
```

## Требования

- Windows PowerShell
- Python 3.12+
- Discord application и bot token
- osu! OAuth client ID и client secret
- локальный или серверный MongoDB
- osu! аккаунт с IRC-доступом для Bancho-бота

## Установка

```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Заполни приватный `.env`. В репозиторий он не добавляется. Файл содержит Discord, osu!, MongoDB и Bancho IRC credentials — не публикуй его и не отправляй в чат.

Для MongoDB можно использовать локальное подключение:

```env
MONGODB_URI=mongodb://127.0.0.1:27017/osu_tourney_dev
```

В Discord Developer Portal должны быть включены `Message Content Intent` и `Server Members Intent`.

## Запуск

### Через визуальный интерфейс

Дважды кликни `start_launcher.vbs`. Откроется окно с кнопками `Запустить`, `Остановить`, `Перезапустить` и окном логов процесса.

Лаунчер использует `venv\\Scripts\\python.exe` и запускает `bot.py` без отдельного окна консоли.

### Автозапуск Windows

Чтобы бот запускался автоматически после входа в Windows, один раз выполни:

```powershell
.\install_autostart.ps1
```

Скрипт создаёт ярлык `OsuTourneyBot.lnk` в папке автозагрузки текущего пользователя. Для отключения:

```powershell
.\remove_autostart.ps1
```

Автозапуск использует отдельный лаунчер и не требует добавления токенов или паролей в ярлык.

### Через PowerShell

```powershell
.\venv\Scripts\Activate.ps1
py bot.py
```

Также доступны:

```powershell
.\Make.ps1 install
.\Make.ps1 run
```

## Pool-команды

```text
/pool_create          создать Draft-пул STD, Taiko или CTB
/pool_create_mania    создать Draft-пул Mania 4K или 7K
/pool_view             просмотреть пул; Draft показывает Submit автору
/pool_edit             изменить карту в Draft или Unranked
/pool_delete           удалить собственный Draft
/pool_list             список пулов с необязательными фильтрами
/pool_formats          требования категорий выбранного режима
/pool_help             справка по pool-командам
/pool_repost_pending   повторно отправить Pending-пулы модераторам
/osu-connect           привязать osu! аккаунт через одноразовый код в osu! PM
```

Названия пулов используются как пользовательский идентификатор. В обычном выводе внутренние ID пулов скрыты.

## Тестовый матч

`/match_create` принимает двух участников текущего Discord-сервера с привязанными osu! аккаунтами, Ranked-пул и формат BO5/BO7/BO9. В Bancho для инвайтов используются подтверждённые `osu_user_id` из MongoDB через формат `#<userid>`, а не Discord-ники или неоднозначный поиск по имени. Бот создаёт MP-комнату, приглашает игроков, ждёт обоих, выполняет ролл и принимает pick/ban только сообщениями со слотом вроде `NM1`.

IRC-данные в `.env`:

```env
BANCHO_USERNAME=your_osu_username
BANCHO_IRC_PASSWORD=your_irc_password
```

Сообщения игрового процесса отправляются только в соответствующий MP-чат Bancho. Discord получает статус матча и результаты.

## Подключение osu! аккаунта

`/osu-connect` выдаёт приватный одноразовый код. Пользователь отправляет его
только в личном сообщении osu! аккаунту из `BANCHO_USERNAME`. После этого бот
получает публичный `osu_user_id` через osu! API и сохраняет связь с Discord ID.
Код хранится в MongoDB только в виде хэша, действует 10 минут и не записывается
в логи. Перед регистрацией матча бот обновляет имя по сохранённому `osu_user_id`,
поэтому смена ника не требует повторной привязки. Для этой команды Bancho IRC поддерживается подключённым даже без
активной MP-комнаты.

## Проверки

Тесты запускаются без подключения к Discord или MongoDB:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

Проверка синтаксиса:

```powershell
.\venv\Scripts\python.exe -m py_compile bot.py database.py bancho_irc.py osu_api.py utils.py cogs\*.py rulesets\*.py
```

## Публикация

Перед отправкой на GitHub проверь:

```powershell
git status
```

В коммит не должны попасть `.env`, `venv/`, логи и локальные базы. Секреты нужно хранить только в локальном `.env` или в секретах CI/CD.
