# Баланс и история операций для Qwen Flask-приложения

## Что входит в архив

- `app.py` — обновлённый сервер на Flask + llama-cpp-python с учётом баланса.
- `templates/balance.html` — страница баланса (`/balance`).
- `templates/transactions.html` — история операций (`/transactions`).
- `database/migration_balance.sql` — SQL-миграция существующей базы.
- `requirements.txt` — Python-зависимости.

Архив не содержит исходные `chat.html`, `login.html`, `registration.html` и файлы фронтенда: их нужно оставить из твоего текущего проекта. Новые HTML-файлы добавляются в существующую папку `templates/`.

## Как работает баланс

- Баланс хранится в `users.balance` (DECIMAL(12,2)).
- Каждая операция записывается в `transactions`.
- Новый пользователь получает баланс `0.00`.
- Один запрос к модели стоит `1.00` внутреннего кредита. Значение меняется в `CHAT_COST` в начале `app.py`.
- Списание выполняется атомарно до генерации. Если средств недостаточно, модель не запускается.
- Если генерация завершается ошибкой или поток прерывается до завершения, приложение пытается вернуть списание отдельной транзакцией.
- Публичного API для произвольного пополнения нет. Пополнять баланс может администратор через MySQL.

Это внутренняя система условных кредитов, не платёжная интеграция и не реальные деньги.

## 1. Установка системных пакетов Debian 12.14

```bash
sudo apt update
sudo apt install -y curl ca-certificates build-essential cmake pkg-config python3-dev libopenblas-dev
```

## 2. Установка uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
uv --version
```

Если команда `uv` не находится после установки, закрой и заново открой терминал либо снова выполни `source "$HOME/.local/bin/env"`.

## 3. Создание и активация venv

Из корня проекта:

```bash
uv venv .venv --python 3.11
source .venv/bin/activate
```

Проверка:

```bash
python --version
which python
```

Путь `which python` должен указывать на `.venv/bin/python`.

## 4. Установка библиотек через uv

```bash
uv pip install -r requirements.txt
```

`llama-cpp-python` может собираться из исходников и устанавливаться несколько минут. Если установка без BLAS прошла, дополнительная сборка не нужна. Если хочешь сборку с OpenBLAS, можно отдельно выполнить:

```bash
CMAKE_ARGS="-DGGML_BLAS=ON -DGGML_BLAS_VENDOR=OpenBLAS" uv pip install --force-reinstall --no-cache-dir llama-cpp-python
```

## 5. Обновление файлов проекта

1. Сделай резервную копию текущего `app.py`.
2. Замени серверный файл на `app.py` из архива.
3. Скопируй `templates/balance.html` и `templates/transactions.html` в существующую папку `templates/`.
4. Не удаляй и не заменяй существующие `chat.html`, `login.html`, `registration.html`, CSS и JS-файлы.

Чтобы открыть страницы из чата, добавь в навигацию существующего `chat.html` ссылки:

```html
<a href="/balance">Баланс</a>
<a href="/transactions">История операций</a>
```

## 6. Миграция MySQL

Сначала сделай резервную копию базы. Затем выбери нужную базу данных и выполни файл `database/migration_balance.sql`.

Например, из консоли MySQL:

```sql
USE sch688_vvedenie;
SOURCE /полный/путь/к/migration_balance.sql;
```

Если используешь phpMyAdmin или другой интерфейс, выбери базу `sch688_vvedenie` и выполни содержимое SQL-файла.

Скрипт добавит `users.balance`, если столбца ещё нет, и создаст `transactions`, если таблицы ещё нет.

## 7. Ручное пополнение тестового баланса

Публичной формы пополнения нет. Для тестового пополнения администратор может выполнить SQL, заменив `1` на реальный `users.id` и `100.00` на нужную сумму:

```sql
START TRANSACTION;
UPDATE users SET balance = balance + 100.00 WHERE id = 1;
INSERT INTO transactions (user_id, amount, type, cost, description, balance_after)
SELECT id, 100.00, 'Пополнение', 0.00, 'Тестовое пополнение администратором', balance
FROM users WHERE id = 1;
COMMIT;
```

Проверь, что UPDATE затронул ровно одну запись. Для тестирования сначала используй тестовый аккаунт.

## 8. Запуск

```bash
source .venv/bin/activate
python3 app.py
```

После входа открой `/balance` и `/transactions`.

## Важные замечания

- В `connect()` оставлена схема прямого подключения к MySQL, как в текущем проекте. Учётные данные не стоит публиковать; после публикации пароля рекомендуется сменить его в MySQL.
- Убедись, что таблица `users` использует InnoDB: `SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='users';`. Для `SELECT ... FOR UPDATE` и транзакций нужен InnoDB.
- Перед выкладкой проверь приложение на копии базы данных.
- При запуске нескольких Gunicorn workers каждый worker загрузит свою копию модели в память. Для этой модели начни с одного worker.
