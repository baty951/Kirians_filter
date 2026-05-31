# Kirians Filter

Телеграм-бот для модерации чатов и каналов на **aiogram 3**.
Хранилище — **PostgreSQL**, антифлуд — **Redis**, разворачивается через **Docker Compose**.

## Возможности

- **Авто-модерация** входящих сообщений: запрещённые слова (текст + regex), ссылки, медиа,
  письменности по Unicode-диапазонам (арабская, китайская, кириллица, иврит, греческая,
  корейская, японская, деванагари, тайская).
- **Антифлуд** на Redis (скользящее окно) с авто-мутом нарушителя.
- **Капча** для новых участников (арифметическая, с авто-киком по таймауту).
- **Команды модерации**: warn / mute / ban / kick / purge и снятие ограничений.
- **Настройки на чат** через inline-меню `/settings` (каждый фильтр включается отдельно).
- **Лог-канал** действий модерации (`/setlog`).

## Команды

| Команда | Действие |
|---|---|
| `/warn [причина]` | Предупреждение; на лимите — авто-мут |
| `/unwarn`, `/warns` | Сброс / просмотр предупреждений |
| `/mute [30m\|2h\|1d] [причина]` | Заглушить |
| `/unmute` | Снять заглушение |
| `/ban [причина]`, `/unban` | Бан / разбан |
| `/kick` | Удалить из чата (с возможностью вернуться) |
| `/purge` | Удалить диапазон сообщений (ответом) |
| `/addword`, `/delword` | Управление списком запрещённых слов |
| `/langs` | Статус языкового фильтра и список письменностей |
| `/blocklang arabic chinese` | Блокировать письменности |
| `/allowlang arabic` | Разблокировать письменности |
| `/settings` | Меню настроек чата |
| `/setlog` | Назначить текущий чат лог-каналом |

Цель команды указывается **ответом на сообщение** либо числовым **ID** пользователя.

## Запуск

```bash
cp .env.example .env        # впишите BOT_TOKEN и пароль БД
docker compose up -d --build
docker compose logs -f bot
```

Бота нужно добавить в чат и выдать права администратора (удаление сообщений,
бан/ограничение участников).

## Деплой (GitHub Actions → VPS по SSH)

При пуше в `main`/`master` workflow `.github/workflows/deploy.yml` подключается
к серверу по SSH, обновляет код и перезапускает контейнеры
(`docker compose up -d --build`). Миграции прогоняются автоматически при старте
контейнера бота.

### 1. Разово подготовить сервер

На VPS должны быть установлены **Docker** и **docker compose**, затем:

```bash
# клонировать репозиторий в каталог деплоя
git clone https://github.com/baty951/Kirians_filter.git /opt/kirians_filter
cd /opt/kirians_filter
cp .env.example .env        # впишите реальный BOT_TOKEN и пароли — этот файл НЕ в git
```

### 2. Добавить SSH-ключ для деплоя

Сгенерируйте отдельную пару ключей (без пароля) и разрешите вход публичным ключом:

```bash
ssh-keygen -t ed25519 -f deploy_key -N ""
ssh-copy-id -i deploy_key.pub user@SERVER_IP   # или вручную в ~/.ssh/authorized_keys
```

Приватный ключ (`deploy_key`) понадобится в секрете `SSH_KEY`.

### 3. Прописать секреты в GitHub

`Settings → Secrets and variables → Actions → New repository secret`:

| Секрет | Значение |
|---|---|
| `SSH_HOST` | IP или домен сервера |
| `SSH_USER` | пользователь SSH (например `deploy` или `root`) |
| `SSH_KEY` | **приватный** ключ целиком (содержимое `deploy_key`) |
| `SSH_PORT` | порт SSH (необязательно; по умолчанию `22`) |
| `DEPLOY_PATH` | путь к репозиторию на сервере, напр. `/opt/kirians_filter` |

После этого любой пуш в `main`/`master` автоматически разворачивает бота.
Запустить деплой вручную можно во вкладке **Actions → Deploy → Run workflow**.

## Подключение к БД через TablePlus (SSH-туннель)

Postgres проброшен только на `127.0.0.1:5432` сервера — из интернета он
недоступен. Подключаемся через встроенный в TablePlus SSH-туннель: наружу
торчит лишь SSH (порт 22).

В TablePlus → **Create a new connection → PostgreSQL**, вкладка соединения:

**Секция Over SSH** (поставьте галочку «Over SSH»):

| Поле | Значение |
|---|---|
| SSH Host | IP/домен сервера |
| SSH Port | `22` |
| SSH User | пользователь SSH |
| SSH Key / Password | ваш SSH-ключ или пароль |

**Секция подключения к БД** (как видно «изнутри» сервера):

| Поле | Значение |
|---|---|
| Host | `127.0.0.1` |
| Port | `5432` |
| User | значение `POSTGRES_USER` из `.env` |
| Password | значение `POSTGRES_PASSWORD` из `.env` |
| Database | значение `POSTGRES_DB` из `.env` |

> Host/Port здесь — это адрес Postgres **на сервере** (туда уходит туннель),
> а не на вашей машине. После настройки нажмите **Test**, затем **Connect**.

Альтернатива без TablePlus-туннеля — пробросить порт вручную и подключаться
к `localhost`:

```bash
ssh -L 5432:localhost:5432 user@SERVER_IP
# теперь в TablePlus: Host 127.0.0.1, Port 5432, без «Over SSH»
```

Не открывайте Postgres на `0.0.0.0` (т.е. `"5432:5432"`) — порт окажется в
интернете и его быстро взломают.

## Архитектура

```
main.py                  точка входа: bot + dispatcher + middlewares
config.py                настройки из .env (pydantic-settings)
bot/
  handlers/              common, admin, moderation, welcome, settings
  filters/content.py     поиск запрещённых слов и ссылок
  middlewares/           db (сессия на апдейт), antiflood (Redis), admin_check
  database/              модели SQLAlchemy + CRUD + async-движок
  utils/                 actions (мут/бан/лог), captcha, helpers
data/badwords/           базовые списки слов по языкам (en, ru, es, de, fr, it, pt, tr, ar)
```

## Миграции БД (Alembic)

Схема управляется Alembic. При `docker compose up` контейнер бота сам прогоняет
`alembic upgrade head` перед стартом (см. `command` в `docker-compose.yml`).

После изменения моделей в `bot/database/models.py`:

```bash
# сгенерировать миграцию (сравнит модели с БД)
docker compose run --rm bot alembic revision --autogenerate -m "описание"
# применить
docker compose run --rm bot alembic upgrade head
# откатить на шаг назад
docker compose run --rm bot alembic downgrade -1
```

`alembic check` показывает, расходятся ли модели с текущей схемой.

## Списки запрещённых слов

`data/badwords/*.txt` — по файлу на язык, по одному корню в строке (строки с `#` —
комментарии). При первом старте все файлы загружаются как **глобальные** слова
(действуют во всех чатах); повторный запуск не дублирует уже добавленные.

Совпадение идёт по **границе слова в начале** корня, без учёта регистра: корень
должен начинать слово. Поэтому `fuck` ловит словоформы `fucking`, `fucker`, но
`ass` уже не срабатывает внутри `class`/`pass`. Граница Unicode-aware — работает
для кириллицы и арабского. Остаются возможны срабатывания, когда корень является
началом легального слова (англ. `ass` → `assume`); такие корни уточняйте.
Паттерны с `is_regex=True` применяются как есть (якоря добавляйте сами). Слова
для конкретного чата добавляются командой `/addword`.

## Заметки

- Локальная разработка без Docker: поднимите PostgreSQL и Redis, задайте
  `POSTGRES_HOST=localhost` / `REDIS_HOST=localhost` в `.env`, выполните
  `alembic upgrade head`, затем `python main.py`.
