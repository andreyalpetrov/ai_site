import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from decimal import Decimal, InvalidOperation
from collections import defaultdict, deque
from functools import wraps

import mysql.connector
from llama_cpp import Llama
from dotenv import load_dotenv
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, stream_with_context, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))


# ----------------------------------------------------------------------------
# Настройки
# ----------------------------------------------------------------------------


# ----------------------------------------------------------------------------
# Локальная GGUF-модель через llama-cpp-python
# ----------------------------------------------------------------------------

REPO_ID = 'bartowski/Qwen2.5-0.5B-Instruct-GGUF'
FILENAME = 'Qwen2.5-0.5B-Instruct-Q4_K_M.gguf'
MODEL_NAME = 'Qwen2.5-0.5B-Instruct-Q4_K_M'

# Стоимость одного успешно запущенного запроса к модели, во внутренних кредитах.
# Измените значение, если хотите другую цену.
CHAT_COST = Decimal('1.00')

print(f'Загрузка модели {FILENAME} из {REPO_ID}...')
llm = Llama.from_pretrained(
    repo_id=REPO_ID,
    filename=FILENAME,
    n_ctx=2048,
    n_threads=8,
    verbose=False,
)
print('Модель успешно загружена.')

MAX_HISTORY_MESSAGES = 30      # сколько последних сообщений диалога отправлять модели
MAX_MESSAGE_LENGTH = 8000      # максимальная длина одного сообщения, символов

app = Flask(__name__)
# Если SECRET_KEY не указан в .env, создаём случайный ключ автоматически.
# При перезапуске приложения такой ключ изменится, и пользователям придётся войти заново.
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 7,   # сессия живёт 7 дней
    MAX_CONTENT_LENGTH=1024 * 1024,                # запросы не больше 1 МБ
)

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)


HASH_NAME = 'pbkdf2_sha256'
HASH_ITERATIONS = 600_000


def hash_password(password, salt=None, iterations=HASH_ITERATIONS):
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return '{}${}${}${}'.format(
        HASH_NAME, iterations,
        base64.b64encode(salt).decode('ascii'),
        base64.b64encode(digest).decode('ascii'))


def check_password(password, stored):
    """True, если пароль подходит к строке из столбца password_hash."""
    try:
        name, iterations, salt_b64, _ = stored.split('$')
        if name != HASH_NAME:
            return False
        expected = hash_password(password, base64.b64decode(salt_b64), int(iterations))
    except (ValueError, AttributeError, TypeError):
        return False
    return hmac.compare_digest(expected, stored)


DUMMY_HASH = hash_password(secrets.token_hex(8))


# ----------------------------------------------------------------------------
# База данных
# ----------------------------------------------------------------------------

def connect():
    return mysql.connector.connect(
        host='185.114.247.43',
        port=3306,
        database='sch688_vvedenie',
        user='sch688_vvedenie',
        password='Qwerty123',
        connection_timeout=10,
    )


def db_find_users(login):
    """Все пользователи, у которых имя или почта совпадают с login."""
    cnx = connect()
    try:
        cur = cnx.cursor()
        cur.execute(
            'SELECT `id`, `username`, `email`, `password_hash` FROM `users` '
            'WHERE `username` = %s OR `email` = %s',
            (login, login))
        return cur.fetchall()
    finally:
        cnx.close()


class RegistrationError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def db_create_user(username, email, password_hash):
    cnx = connect()
    try:
        cur = cnx.cursor()

        # 1. Имя и почта не должны быть заняты
        cur.execute(
            'SELECT `username`, `email` FROM `users` '
            'WHERE `username` = %s OR `email` = %s '
            'OR `username` = %s OR `email` = %s',
            (username, email, email, username))
        for row in cur.fetchall():
            if str(row[1]).lower() == email.lower():
                raise RegistrationError('Эта почта уже зарегистрирована.', 409)
            raise RegistrationError('Это имя уже занято.', 409)

        # 2. Добавляем пользователя (тот же запрос, что и в исходном примере)
        cur.execute(
            'INSERT INTO `users`(`username`, `email`, `password_hash`) VALUES (%s, %s, %s)',
            (username, email, password_hash))
        cnx.commit()
        new_user_id = cur.lastrowid

        # 3. Устройство таблицы неизвестно, поэтому проверяем, что хеш записался
        #    целиком. Если столбец оказался слишком коротким и база обрезала
        #    строку, убираем только что добавленную нерабочую запись.
        cur.execute(
            'SELECT `password_hash` FROM `users` WHERE `username` = %s AND `email` = %s',
            (username, email))
        saved = [row[0] for row in cur.fetchall()]
        if password_hash not in saved:
            for broken in saved:
                if broken and password_hash.startswith(str(broken)):
                    cur.execute(
                        'DELETE FROM `users` WHERE `username` = %s AND `email` = %s '
                        'AND `password_hash` = %s',
                        (username, email, broken))
            cnx.commit()
            raise RegistrationError(
                'База данных обрезала хеш пароля: столбец password_hash слишком короткий '
                '(нужно не меньше {} символов).'.format(len(password_hash)), 500)
        return new_user_id
    finally:
        cnx.close()


def db_error_message(err):
    app.logger.error('Ошибка базы данных: %s', err)
    if getattr(err, 'errno', None) == 1062:      # нарушение уникальности
        return 'Такое имя или почта уже зарегистрированы.', 409
    if getattr(err, 'errno', None) == 1406:      # Data too long
        return ('Значение не помещается в столбец таблицы users '
                '(слишком длинное имя, почта или хеш пароля).'), 500
    return 'База данных недоступна. Попробуйте позже.', 503


class InsufficientBalance(Exception):
    def __init__(self, current_balance):
        self.current_balance = current_balance
        super().__init__('Недостаточно средств на балансе.')


def change_balance(user_id, amount, transaction_type, cost, description, require_funds=False):
    """Атомарно меняет баланс и записывает операцию в transactions."""
    amount = Decimal(str(amount)).quantize(Decimal('0.01'))
    cost = Decimal(str(cost)).quantize(Decimal('0.01'))
    cnx = connect()
    try:
        cur = cnx.cursor(dictionary=True)
        cur.execute('SELECT balance FROM users WHERE id = %s FOR UPDATE', (user_id,))
        user = cur.fetchone()
        if not user:
            cnx.rollback()
            raise RegistrationError('Пользователь не найден.', 404)

        current = Decimal(str(user['balance'] or '0.00')).quantize(Decimal('0.01'))
        updated = (current + amount).quantize(Decimal('0.01'))
        if require_funds and updated < 0:
            cnx.rollback()
            raise InsufficientBalance(current)
        if updated < 0:
            cnx.rollback()
            raise InsufficientBalance(current)

        cur.execute('UPDATE users SET balance = %s WHERE id = %s', (updated, user_id))
        cur.execute(
            """INSERT INTO transactions
               (user_id, amount, type, cost, description, balance_after)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (user_id, amount, transaction_type, cost, description, updated))
        cnx.commit()
        return updated
    except Exception:
        cnx.rollback()
        raise
    finally:
        cnx.close()


def db_get_balance_user(user_id):
    cnx = connect()
    try:
        cur = cnx.cursor(dictionary=True)
        cur.execute(
            'SELECT id, username, email, balance FROM users WHERE id = %s',
            (user_id,))
        return cur.fetchone()
    finally:
        cnx.close()


def db_get_transactions(user_id):
    cnx = connect()
    try:
        cur = cnx.cursor(dictionary=True)
        cur.execute(
            """SELECT id, amount, type, cost, description, balance_after, created_at
               FROM transactions WHERE user_id = %s
               ORDER BY created_at DESC, id DESC""",
            (user_id,))
        return cur.fetchall()
    finally:
        cnx.close()


# ----------------------------------------------------------------------------
# Защита от перебора паролей: не больше 10 неудачных входов за 10 минут с одного IP
# ----------------------------------------------------------------------------

FAILED_LIMIT = 10
FAILED_WINDOW = 600
_failed_logins = defaultdict(deque)
_failed_lock = threading.Lock()


def too_many_failures(ip):
    now = time.time()
    with _failed_lock:
        attempts = _failed_logins[ip]
        while attempts and now - attempts[0] > FAILED_WINDOW:
            attempts.popleft()
        return len(attempts) >= FAILED_LIMIT


def remember_failure(ip):
    with _failed_lock:
        _failed_logins[ip].append(time.time())


# ----------------------------------------------------------------------------
# Страницы
# ----------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            if request.path.startswith('/api/'):
                return jsonify(error='Нужно войти в аккаунт.'), 401
            return redirect(url_for('login_page'))
        return view(*args, **kwargs)
    return wrapper


@app.get('/')
@login_required
def chat_page():
    return render_template('chat.html', username=session['user'], model=MODEL_NAME)


@app.get('/login')
def login_page():
    if 'user' in session:
        return redirect(url_for('chat_page'))
    return render_template('login.html', model=MODEL_NAME)


@app.get('/register')
def register_page():
    if 'user' in session:
        return redirect(url_for('chat_page'))
    return render_template('registration.html', model=MODEL_NAME)


@app.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))


@app.get('/balance')
@login_required
def balance_page():
    try:
        user = db_get_balance_user(session['user_id'])
    except mysql.connector.Error as err:
        app.logger.exception('Не удалось загрузить баланс')
        return 'Не удалось загрузить баланс. Попробуйте позже.', 503
    if not user:
        session.clear()
        return redirect(url_for('login_page'))
    return render_template('balance.html', user=user)


@app.get('/transactions')
@login_required
def transactions_page():
    try:
        transactions = db_get_transactions(session['user_id'])
    except mysql.connector.Error:
        app.logger.exception('Не удалось загрузить историю транзакций')
        return 'Не удалось загрузить историю операций. Попробуйте позже.', 503
    return render_template('transactions.html', transactions=transactions)


@app.get('/api/balance')
@login_required
def api_balance():
    try:
        user = db_get_balance_user(session['user_id'])
    except mysql.connector.Error:
        app.logger.exception('Не удалось получить баланс через API')
        return jsonify(error='Не удалось получить баланс.'), 503
    if not user:
        session.clear()
        return jsonify(error='Пользователь не найден.'), 404
    return jsonify(balance=str(user['balance'] or Decimal('0.00')))


# ----------------------------------------------------------------------------
# Регистрация и вход
# ----------------------------------------------------------------------------

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def start_session(username, email, user_id):
    session.clear()
    session.permanent = True
    session['user'] = username
    session['email'] = email
    session['user_id'] = int(user_id)


@app.post('/api/register')
def api_register():
    req = request.get_json(silent=True) or {}
    name = str(req.get('name', '')).strip()
    email = str(req.get('email', '')).strip().lower()
    password = str(req.get('password', ''))

    if not 2 <= len(name) <= 50:
        return jsonify(error='Имя должно быть длиной от 2 до 50 символов.'), 400
    if '@' in name:
        return jsonify(error='В имени нельзя использовать символ @.'), 400
    if len(email) > 100 or not EMAIL_RE.match(email):
        return jsonify(error='Укажите корректный адрес почты.'), 400
    if not 8 <= len(password) <= 200:
        return jsonify(error='Пароль должен быть не короче 8 символов.'), 400

    try:
        user_id = db_create_user(name, email, hash_password(password))
    except RegistrationError as err:
        return jsonify(error=err.message), err.status
    except mysql.connector.Error as err:
        message, status = db_error_message(err)
        return jsonify(error=message), status

    start_session(name, email, user_id)
    return jsonify(ok=True)


@app.post('/api/login')
def api_login():
    req = request.get_json(silent=True) or {}
    login = str(req.get('login', '')).strip()
    password = str(req.get('password', ''))
    ip = request.remote_addr or 'unknown'

    if not login or not password:
        return jsonify(error='Введите логин и пароль.'), 400
    if too_many_failures(ip):
        return jsonify(error='Слишком много неудачных попыток. Подождите 10 минут.'), 429

    try:
        rows = db_find_users(login)
    except mysql.connector.Error as err:
        message, status = db_error_message(err)
        return jsonify(error=message), status

    user = None
    for user_id, username, email, password_hash in rows:
        if check_password(password, password_hash):
            user = (username, email, user_id)
            break
    if not rows:
        check_password(password, DUMMY_HASH)

    if user is None:
        remember_failure(ip)
        return jsonify(error='Неверный логин или пароль.'), 401

    start_session(*user)
    return jsonify(ok=True)


# ----------------------------------------------------------------------------
# Чат
# ----------------------------------------------------------------------------

class ThinkTagSplitter:

    def __init__(self):
        self.buffer = ''
        self.in_think = False

    def feed(self, text):
        result = []
        self.buffer += text
        while True:
            tag = '</think>' if self.in_think else '<think>'
            kind = 'thinking' if self.in_think else 'content'
            pos = self.buffer.find(tag)
            if pos >= 0:
                if pos:
                    result.append((kind, self.buffer[:pos]))
                self.buffer = self.buffer[pos + len(tag):]
                self.in_think = not self.in_think
                continue
            # В конце может оказаться начало тега ("<thi") - придержим его до следующего куска
            keep = 0
            for size in range(min(len(tag) - 1, len(self.buffer)), 0, -1):
                if tag.startswith(self.buffer[-size:]):
                    keep = size
                    break
            ready = self.buffer[:len(self.buffer) - keep]
            if ready:
                result.append((kind, ready))
            self.buffer = self.buffer[len(self.buffer) - keep:]
            return result

    def flush(self):
        rest, self.buffer = self.buffer, ''
        return [('thinking' if self.in_think else 'content', rest)] if rest else []


def clean_messages(raw):
    """Оставляет от присланной истории только то, что можно передать модели."""
    if not isinstance(raw, list):
        return []
    messages = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role, content = item.get('role'), item.get('content')
        if role not in ('user', 'assistant') or not isinstance(content, str):
            continue
        content = content.strip()
        if content:
            messages.append({'role': role, 'content': content[:MAX_MESSAGE_LENGTH]})
    messages = messages[-MAX_HISTORY_MESSAGES:]
    while messages and messages[0]['role'] != 'user':
        messages.pop(0)
    return messages


def event(kind, text=''):
    """Одна строка потока, который читает chat.js."""
    return json.dumps({'type': kind, 'text': text}, ensure_ascii=False) + '\n'


# Модель работает в один поток, поэтому отвечаем только на один запрос одновременно
generation_lock = threading.Lock()


@app.post('/api/chat')
@login_required
def api_chat():
    req = request.get_json(silent=True) or {}
    messages = clean_messages(req.get('messages'))
    if not messages or messages[-1]['role'] != 'user':
        return jsonify(error='Пустое сообщение.'), 400

    def generate():
        if not generation_lock.acquire(blocking=False):
            yield event('error', 'Модель сейчас отвечает на другой запрос. Попробуйте чуть позже.')
            return

        charged = False
        completed = False
        generation_failed = False
        try:
            try:
                balance_after = change_balance(
                    session['user_id'],
                    -CHAT_COST,
                    'Списание',
                    CHAT_COST,
                    'Запрос к нейросети',
                    require_funds=True,
                )
                charged = True
            except InsufficientBalance as err:
                yield event(
                    'error',
                    'Недостаточно кредитов. Баланс: {}. Стоимость запроса: {}.'.format(
                        err.current_balance, CHAT_COST))
                return
            except mysql.connector.Error:
                app.logger.exception('Не удалось списать стоимость запроса')
                yield event('error', 'Не удалось проверить баланс. Попробуйте позже.')
                return

            stream = llm.create_chat_completion(
                messages=messages,
                temperature=0.6,
                max_tokens=1024,
                stream=True,
            )

            splitter = ThinkTagSplitter()
            for chunk in stream:
                choices = chunk.get('choices') or []
                if not choices:
                    continue
                delta = choices[0].get('delta') or {}
                content = delta.get('content')
                if content:
                    for kind, text in splitter.feed(content):
                        yield event(kind, text)

            for kind, text in splitter.flush():
                yield event(kind, text)
            completed = True
            yield event('done')

        except GeneratorExit:
            # Если клиент прервал поток до завершения генерации, списание вернётся в finally.
            raise
        except Exception:
            app.logger.exception('Ошибка генерации через llama-cpp-python')
            generation_failed = True
        finally:
            if charged and not completed:
                try:
                    change_balance(
                        session['user_id'], CHAT_COST, 'Возврат', Decimal('0.00'),
                        'Возврат за незавершённый запрос к нейросети')
                except Exception:
                    app.logger.exception('Не удалось вернуть списанные кредиты')
            generation_lock.release()

        if generation_failed:
            yield event('error', 'Произошла ошибка при генерации. Списание возвращено, если оно прошло.')

    return Response(
        stream_with_context(generate()),
        mimetype='application/x-ndjson',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


if __name__ == '__main__':
    # Запуск для проверки: python app.py
    # Для постоянной работы используйте gunicorn (см. README.md)
    app.run(host='127.0.0.1', port=8000, threaded=True)
