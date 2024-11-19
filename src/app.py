from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
import sqlite3
import requests
from datetime import datetime, timedelta
import json
import telebot
from telebot.apihelper import ApiException
from yoomoney import Client, Quickpay
from apscheduler.schedulers.background import BackgroundScheduler
from pytz import utc
from functools import wraps
import threading
import time
import sys
import signal
import uuid
import os
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# Добавим глобальную переменную для планировщика
scheduler = None

# В начало файла добавим глобальные переменные
telegram_bot = None
bot_thread = None
bot_running = False  # Добавляем флаг состояния бота

# Добавим константы для загрузки файлов
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# Создадим папку для загрузок, если её нет
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def create_payment_for_client(email, amount, days, inbound_id, tgid=None):
    """
    Создает платеж для клиента
    
    Args:
        email (str): Email клиента
        amount (float): Сумма платежа
        days (int): Количество дней
        inbound_id (str): ID inbound
        tgid (str, optional): Telegram ID клиента
        
    Returns:
        dict: Результат создания платежа
    """
    try:
        yoomoney_settings = get_yoomoney_settings()
        if not yoomoney_settings or not yoomoney_settings['is_enabled']:
            raise Exception('YooMoney не настроен')
        
        # Создаем уникальный ID платежа
        payment_id = f"vpn_{email}_{int(datetime.now().timestamp())}"
        
        # Создаем форму оплаты
        quickpay = Quickpay(
            receiver=yoomoney_settings['wallet_id'],
            quickpay_form="shop",
            targets=f"Продление VPN для {email} на {days} дней",
            paymentType="SB",
            sum=amount,
            label=payment_id,
            successURL=yoomoney_settings['redirect_url']
        )
        
        # Сохраняем информацию о платеже
        db = get_db()
        try:
            db.execute('''INSERT INTO payments 
                         (email, amount, days, payment_id, inbound_id) 
                         VALUES (?, ?, ?, ?, ?)''',
                      [email, amount, days, payment_id, inbound_id])
            db.commit()
        finally:
            db.close()
        
        return {
            'success': True,
            'payment_url': quickpay.redirected_url,
            'payment_id': payment_id
        }
        
    except Exception as e:
        print(f"Error creating payment: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }

def get_db():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with app.app_context():
        db = get_db()
        with app.open_resource('schema.sql', mode='r') as f:
            db.cursor().executescript(f.read())
        db.commit()

def get_settings():
    db = get_db()
    settings = db.execute('SELECT * FROM settings ORDER BY id DESC LIMIT 1').fetchone()
    db.close()
    return settings

def get_telegram_settings():
    db = get_db()
    settings = db.execute('SELECT * FROM telegram_settings ORDER BY id DESC LIMIT 1').fetchone()
    db.close()
    return settings

def get_yoomoney_settings():
    db = get_db()
    settings = db.execute('SELECT * FROM yoomoney_settings ORDER BY id DESC LIMIT 1').fetchone()
    db.close()
    return settings

def get_client_data(email):
    db = get_db()
    client = db.execute('SELECT * FROM client_data WHERE email = ?', [email]).fetchone()
    db.close()
    return client

def update_client_data(email, tgid):
    db = get_db()
    existing = db.execute('SELECT * FROM client_data WHERE email = ?', [email]).fetchone()
    if existing:
        db.execute('UPDATE client_data SET tgid = ?, updated_at = CURRENT_TIMESTAMP WHERE email = ?',
                  [tgid, email])
    else:
        db.execute('INSERT INTO client_data (email, tgid) VALUES (?, ?)',
                  [email, tgid])
    db.commit()
    db.close()

# Добавим декоратор для проверки авторизации
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ? AND password = ?',
                         [username, password]).fetchone()
        db.close()
        
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('index'))
        
        return render_template('login.html', error='Неверный логин или пароль')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form['current_password']
        new_username = request.form['new_username']
        new_password = request.form['new_password']
        confirm_password = request.form['confirm_password']
        
        if new_password != confirm_password:
            return render_template('change_password.html', 
                                 error='Пароли не совпадают')
        
        db = get_db()
        # Проверяем текущий пароль
        user = db.execute('SELECT * FROM users WHERE id = ? AND password = ?',
                         [session['user_id'], current_password]).fetchone()
        
        if not user:
            return render_template('change_password.html', 
                                 error='Неверный теущий пароль')
        
        # Проверяем, не занят ли новый логин другим пользователем
        if new_username != user['username']:
            existing_user = db.execute('SELECT * FROM users WHERE username = ? AND id != ?',
                                     [new_username, session['user_id']]).fetchone()
            if existing_user:
                return render_template('change_password.html', 
                                     error='Этот логин уже занят')
        
        try:
            # Обновляем учетные данные существующего пользователя
            db.execute('UPDATE users SET username = ?, password = ? WHERE id = ?',
                      [new_username, new_password, session['user_id']])
            db.commit()
            
            # Обновляем данные сессии
            session['username'] = new_username
            
            return render_template('change_password.html', 
                                 success='Учетные данные успешно обновлены')
        except Exception as e:
            db.rollback()
            return render_template('change_password.html', 
                                 error=f'Ошибка при обновлении данных: {str(e)}')
        finally:
            db.close()
    
    return render_template('change_password.html')

# авляем декоратор @login_required ко всем существующим маршрутам
@app.route('/')
@login_required
def index():
    return redirect(url_for('clients'))

@app.route('/clients')
@login_required
def clients():
    settings = get_settings()
    if not settings:
        flash('Пожалуйста, сначала настройте параметры подключения')
        return redirect(url_for('settings'))
    
    try:
        session = requests.Session()
        login_response = session.post(
            f"{settings['panel_url']}/login",
            data={'username': settings['username'], 'password': settings['password']}
        )
        
        if login_response.status_code != 200:
            flash('Ошибка авторизации')
            return redirect(url_for('settings'))
        
        clients_response = session.get(f"{settings['panel_url']}/panel/api/inbounds/list")
        clients_data = clients_response.json()
        
        if not clients_data['success']:
            flash('Ошибка получения списка клиентов')
            return redirect(url_for('settings'))

        # Получаем все локальные данные клиентов
        db = get_db()
        local_clients = {}
        for row in db.execute('SELECT email, tgid FROM client_data'):
            local_clients[row['email']] = row['tgid']  # Исправлено: добавлена закрывающая скобка и правильный ��интаксис присваивания
        db.close()

        # Объединяем данные и добавляем UUID клиентов
        for inbound in clients_data['obj']:
            if 'settings' in inbound:
                try:
                    settings_json = json.loads(inbound['settings'])
                    clients_map = {client['email']: client['id'] for client in settings_json.get('clients', [])}
                    
                    # Добавляем UUID и tgid к данным клиентов
                    for client in inbound['clientStats']:
                        client['tgid'] = local_clients.get(client['email'])
                        client['uuid'] = clients_map.get(client['email'])
                except (json.JSONDecodeError, KeyError):
                    continue
        
        return render_template('clients.html', 
                             clients=clients_data['obj'],
                             now=datetime.now().timestamp())
        
    except requests.exceptions.RequestException as e:
        flash(f'Ошибка подключения к панели: {str(e)}')
        return redirect(url_for('settings'))

@app.route('/telegram/settings', methods=['GET', 'POST'])
@login_required
def telegram_settings():
    if request.method == 'POST':
        form_type = request.form.get('form_type')
        
        # Создаем новое подключение к базе данных
        db = get_db()
        
        try:
            # Начинаем транзакцию
            db.execute('BEGIN IMMEDIATE')
            
            # Проверяем и добавляем новые колонки если их нет
            try:
                db.execute('SELECT notify_days FROM telegram_settings LIMIT 1')
            except sqlite3.OperationalError:
                db.execute('ALTER TABLE telegram_settings ADD COLUMN notify_days INTEGER DEFAULT 3')
                
            try:
                db.execute('SELECT create_payment FROM telegram_settings LIMIT 1')
            except sqlite3.OperationalError:
                db.execute('ALTER TABLE telegram_settings ADD COLUMN create_payment BOOLEAN DEFAULT 0')
                
            try:
                db.execute('SELECT payment_amount FROM telegram_settings LIMIT 1')
            except sqlite3.OperationalError:
                db.execute('ALTER TABLE telegram_settings ADD COLUMN payment_amount DECIMAL(10,2)')
                
            try:
                db.execute('SELECT notification_template FROM telegram_settings LIMIT 1')
            except sqlite3.OperationalError:
                db.execute('ALTER TABLE telegram_settings ADD COLUMN notification_template TEXT')
                
            try:
                db.execute('SELECT check_interval FROM telegram_settings LIMIT 1')
            except sqlite3.OperationalError:
                db.execute('ALTER TABLE telegram_settings ADD COLUMN check_interval INTEGER DEFAULT 60')
                
            try:
                db.execute('SELECT interval_unit FROM telegram_settings LIMIT 1')
            except sqlite3.OperationalError:
                db.execute('ALTER TABLE telegram_settings ADD COLUMN interval_unit TEXT DEFAULT "minutes"')
            
            if form_type == 'bot_settings':
                bot_token = request.form['bot_token']
                admin_chat_id = request.form['admin_chat_id']
                is_enabled = 1 if 'is_enabled' in request.form else 0
                
                db.execute('''INSERT INTO telegram_settings 
                             (bot_token, admin_chat_id, is_enabled) 
                             VALUES (?, ?, ?)''',
                          [bot_token, admin_chat_id, is_enabled])
                
                flash('Настройки Telegram бота успешно сохранены')
                
            elif form_type == 'notification_settings':
                notify_days = request.form['notify_days']
                create_payment = 1 if 'create_payment' in request.form else 0
                payment_amount = request.form.get('payment_amount')
                notification_template = request.form['notification_template']
                check_interval = request.form['check_interval']
                interval_unit = request.form['interval_unit']
                
                # Обновляем настройки уведомлений
                db.execute('''UPDATE telegram_settings 
                             SET notify_days = ?, 
                                 create_payment = ?, 
                                 payment_amount = ?,
                                 notification_template = ?,
                                 check_interval = ?,
                                 interval_unit = ?
                             WHERE id = (SELECT MAX(id) FROM telegram_settings)''',
                          [notify_days, create_payment, payment_amount, notification_template, 
                           check_interval, interval_unit])
                
                flash('Настройки уведомлений успешно сохранены')
            
            # Фиксируем изменения
            db.commit()
            
            # Перезапускаем планировщик с новым интервалом
            restart_scheduler()
            
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                flash('База данных занята, попробуйте позже', 'error')
            else:
                flash(f'Ошибка базы данных: {str(e)}', 'error')
            db.rollback()
        except Exception as e:
            flash(f'Ошибка при сохранении: {str(e)}', 'error')
            db.rollback()
        finally:
            db.close()
        
        return redirect(url_for('telegram_settings'))
    
    # Получаем текущие настройки
    try:
        db = get_db()
        settings = db.execute('SELECT * FROM telegram_settings ORDER BY id DESC LIMIT 1').fetchone()
        db.close()
    except Exception as e:
        flash(f'Ошибка при поучении настроек: {str(e)}', 'error')
        settings = None
    
    return render_template('telegram_settings.html', telegram_settings=settings)

@app.route('/telegram/test', methods=['POST'])
@login_required
def test_telegram():
    settings = get_telegram_settings()
    if not settings:
        return jsonify({'success': False, 'error': 'Настойки бота не найдены'})
    
    try:
        bot = telebot.TeleBot(settings['bot_token'])
        if not settings['admin_chat_id']:
            return jsonify({'success': False, 'error': 'ID администратора не указан'})
            
        message = "🟢 Тестовое сообщение\nБот успешно настроен и работает!"
        bot.send_message(settings['admin_chat_id'], message)
        return jsonify({'success': True})
        
    except ApiException as e:
        return jsonify({'success': False, 'error': str(e)})
    except Exception as e:
        return jsonify({'success': False, 'error': f'Неизвестная ошибка: {str(e)}'})

@app.route('/yoomoney/settings', methods=['GET', 'POST'])
@login_required
def yoomoney_settings():
    if request.method == 'POST':
        wallet_id = request.form['wallet_id']
        secret_key = request.form['secret_key']
        redirect_url = request.form['redirect_url']
        is_enabled = 1 if 'is_enabled' in request.form else 0
        
        db = get_db()
        db.execute('''INSERT INTO yoomoney_settings 
                     (wallet_id, secret_key, redirect_url, is_enabled) 
                     VALUES (?, ?, ?, ?)''',
                  [wallet_id, secret_key, redirect_url, is_enabled])
        db.commit()
        db.close()
        
        flash('Настройки YooMoney успешно сохранены')
        return redirect(url_for('yoomoney_settings'))
    
    settings = get_yoomoney_settings()
    return render_template('yoomoney_settings.html', yoomoney_settings=settings)

@app.route('/yoomoney/test', methods=['POST'])
@login_required
def test_yoomoney():
    settings = get_yoomoney_settings()
    if not settings:
        return jsonify({'success': False, 'error': 'Настройки YooMoney не найдены'})
    
    try:
        client = Client(settings['secret_key'])
        account_info = client.account_info()
        
        return jsonify({
            'success': True,
            'balance': account_info.balance,
            'account': account_info.account,
            'currency': account_info.currency
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/telegram/send_stats', methods=['POST'])
@login_required
def send_stats():
    settings = get_telegram_settings()
    panel_settings = get_settings()
    if not settings or not settings['is_enabled']:
        return jsonify({'success': False, 'error': 'Telegram бот не нстроен или тключен'})
    
    try:
        data = request.json
        tgid = data['tgid']
        email = data['email']
        traffic_up = float(data.get('traffic_up', 0))
        traffic_down = float(data.get('traffic_down', 0))
        total = float(data['total']) if data['total'] != '∞' else 0
        expiry_time = data['expiryTime']
        inbound_id = data['inbound_id']  # Добавляем получение inbound_id
        
        # Конвертируем MB в GB
        traffic_up_gb = traffic_up / 1024
        traffic_down_gb = traffic_down / 1024
        
        # Формируем собщение со статистикой
        message = (
            f"📊 Статистика пользователя: {email}\n\n"
            f"📤 Отправлено: {traffic_up_gb:.2f} GB\n"
            f"📥 Скачао: {traffic_down_gb:.2f} GB\n"
        )
        
        if total > 0:
            total_gb = total / (1024 * 1024 * 1024)
            message += f"💾 Лимит трафика: {total_gb:.2f} GB\n"
        else:
            message += "💾 Лимит трафк: ∞\n"
        
        # Проверяем срок действия
        if expiry_time and expiry_time != '0':
            current_time = datetime.now().timestamp() * 1000
            time_left = float(expiry_time) - current_time
            days_left = int(time_left / (1000 * 60 * 60 * 24))
            hours_left = int((time_left % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60))
            
            if days_left > 0:
                message += f"⏳ До окончания подписки: {days_left} дн. {hours_left} ч.\n\n"
            elif hours_left > 0:
                message += f"⏳ До окончания подписки: {hours_left} ч.\n\n"
            else:
                message += "⏳ Пописка истекла\n\n"
        else:
            message += " Срок действия: бессрочно\n\n"

        # Полчаем ссылку для подклюения
        session = requests.Session()
        
        # Логин
        login_response = session.post(
            f"{panel_settings['panel_url']}/login",
            data={'username': panel_settings['username'], 'password': panel_settings['password']}
        )
        
        if login_response.status_code != 200:
            return jsonify({'success': False, 'error': 'Ошибка авторизации'})
        
        # Получаем данные inbound
        inbounds_response = session.get(f"{panel_settings['panel_url']}/panel/api/inbounds/list")
        inbounds_data = inbounds_response.json()
        
        if not inbounds_data['success']:
            return jsonify({'success': False, 'error': 'Ошибка получения данных'})
        
        # Формируем ссылку для подключения
        for inbound in inbounds_data['obj']:
            if str(inbound['id']) == str(inbound_id):
                settings_json = json.loads(inbound['settings'])
                stream_settings = json.loads(inbound['streamSettings'])
                
                # Ищем клиента по email
                client_id = None
                client_flow = None
                for client in settings_json.get('clients', []):
                    if client['email'] == email:
                        client_id = client['id']
                        client_flow = client.get('flow', 'xtls-rprx-vision')
                        break
                
                if client_id:
                    # Извлеаем домен из URL панели
                    panel_url = panel_settings['panel_url']
                    domain_part = panel_url.split('://')[-1]
                    domain = domain_part.split('/')[0].split(':')[0]
                    
                    # Получаем параметы для ссыл
                    tcp = stream_settings.get('network', '')
                    reality = stream_settings.get('security', '')
                    
                    # раьно получни publicKey
                    pbk = None
                    if 'realitySettings' in stream_settings:
                        pbk = stream_settings['realitySettings'].get('publicKey')
                    if not pbk and 'settings' in stream_settings.get('realitySettings', {}):
                        pbk = stream_settings['realitySettings']['settings'].get('publicKey')
                    
                    # Добавляем отладочный вывод
                    print("Stream Settings:", json.dumps(stream_settings, indent=2))
                    print("Public Key:", pbk)
                    
                    reality_settings = stream_settings.get('realitySettings', {})
                    sid = reality_settings.get('shortIds', [''])[0]
                    server_name = reality_settings.get('serverNames', [''])[0]
                    port = inbound.get('port', '')
                    
                    # ормруем ссылку только если сть все необходимые параметры
                    if not pbk:
                        return jsonify({'success': False, 'error': 'Не уась получить publicKey'})
                    
                    # Формируем ссылку
                    params = [
                        f"type={tcp}",
                        f"security={reality}",
                        f"pbk={pbk}",  # Теперь pbk очно не будет пустым
                        "fp=chrome",
                        f"sni={server_name}",
                        f"sid={sid}",
                        "spx=%2F"
                    ]
                    
                    if client_flow:
                        params.append(f"flow={client_flow}")
                    
                    link = f"vless://{client_id}@{domain}:{port}?{'&'.join(params)}#vless2-{email}"
                    
                    # Добавляем ссылку в сообщение, оборачивая её в теги code
                    message += f"🔗 Ссылка для подключения:\n<code>{link}</code>"
        
        # Отправляем сообщение с поддержкой HTML
        bot = telebot.TeleBot(settings['bot_token'])
        bot.send_message(tgid, message, parse_mode='HTML')
        
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/clients/add', methods=['POST'])
@login_required
def add_client():
    settings = get_settings()
    if not settings:
        return jsonify({'success': False, 'error': 'Настройки панели не найдены'})
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Данные не получены'})

        session = requests.Session()
        
        # Логин
        login_response = session.post(
            f"{settings['panel_url']}/login",
            data={'username': settings['username'], 'password': settings['password']}
        )
        
        if login_response.status_code != 200:
            return jsonify({'success': False, 'error': 'Ошибка авторизации'})
        
        # Добавление клиента
        headers = {'Content-Type': 'application/json'}
        
        add_response = session.post(
            f"{settings['panel_url']}/panel/api/inbounds/addClient",
            json=data,  # Отправляем анные как есть
            headers=headers
        )
        
        if add_response.status_code != 200:
            return jsonify({'success': False, 'error': f'Ошибка сервера: {add_response.status_code}'})
            
        try:
            response_data = add_response.json()
        except ValueError:
            return jsonify({'success': False, 'error': 'Некорректный ответ от сервера'})
            
        if not response_data.get('success', False):
            return jsonify({'success': False, 'error': response_data.get('msg', 'Неизестная ошибка')})
        
        # Сохраняем Telegram ID в локальной базе если он указан
        client_settings = json.loads(data['settings'])
        if client_settings['clients'][0].get('tgId'):
            update_client_data(
                client_settings['clients'][0]['email'],
                client_settings['clients'][0]['tgId']
            )
        
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/clients/update', methods=['POST'])
@login_required
def update_client():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Данные не получены'})

        email = data['email']
        tgid = data.get('tgid', '')
        
        # Обновляем тольо окальные данны
        try:
            update_client_data(email, tgid)
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'error': f'Ошибка обновления локальных данных: {str(e)}'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/clients/delete', methods=['POST'])
@login_required
def delete_client():
    settings = get_settings()
    if not settings:
        return jsonify({'success': False, 'error': 'астройки панели не найдены'})
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Данные е получеы'})

        inbound_id = data['inbound_id']
        client_uuid = data['uuid']  # Теперь ожидам UUID вместо email
        email = data['email']  # Email нужен только для удалени из локальной БД
        
        session = requests.Session()
        
        # Логин
        login_response = session.post(
            f"{settings['panel_url']}/login",
            data={'username': settings['username'], 'password': settings['password']}
        )
        
        if login_response.status_code != 200:
            return jsonify({'success': False, 'error': 'Ошибка авторизации'})
        
        # Удаление клиента используя правиьный URL с UUID
        delete_response = session.post(
            f"{settings['panel_url']}/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}"
        )
        
        if delete_response.status_code != 200:
            return jsonify({'success': False, 'error': f'Ошибка сервера: {delete_response.status_code}'})
            
        try:
            response_data = delete_response.json()
        except ValueError:
            return jsonify({'success': False, 'error': 'Некорректный твет от сервера'})
            
        if not response_data.get('success', False):
            return jsonify({'success': False, 'error': response_data.get('msg', 'Неизвестная ошибка')})
        
        # Удаляем локальные анные
        db = get_db()
        db.execute('DELETE FROM client_data WHERE email = ?', [email])
        db.commit()
        db.close()
        
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/clients/get_link', methods=['POST'])
@login_required
def get_client_link():
    settings = get_settings()
    if not settings:
        return jsonify({'success': False, 'error': 'Настройки панели не найдены'})
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Данные не получены'})

        inbound_id = data['inbound_id']
        email = data['email']
        
        # Извлекаем доен  порт из URL панели
        panel_url = settings['panel_url']
        domain_part = panel_url.split('://')[-1]
        domain_port = domain_part.split('/')[0].split(':')[0]
        
        session = requests.Session()
        
        # Логин
        login_response = session.post(
            f"{settings['panel_url']}/login",
            data={'username': settings['username'], 'password': settings['password']}
        )
        
        if login_response.status_code != 200:
            return jsonify({'success': False, 'error': 'Ошибка авторизации'})
        
        # Получаем данные inbound
        inbounds_response = session.get(f"{settings['panel_url']}/panel/api/inbounds/list")
        inbounds_data = inbounds_response.json()
        
        if not inbounds_data['success']:
            return jsonify({'success': False, 'error': 'Ошибка получения данных'})
        
        # Ищем нужный inbound
        for inbound in inbounds_data['obj']:
            if str(inbound['id']) == str(inbound_id):
                settings_json = json.loads(inbound['settings'])
                stream_settings = json.loads(inbound['streamSettings'])
                
                # Ищем клиента по email
                client_id = None
                client_flow = None
                for client in settings_json.get('clients', []):
                    if client['email'] == email:
                        client_id = client['id']
                        client_flow = client.get('flow', 'xtls-rprx-vision')
                        break
                
                if client_id:
                    # Плуаем все необходимые параметры
                    tcp = stream_settings.get('network', '')
                    reality = stream_settings.get('security', '')
                    reality_settings = stream_settings.get('realitySettings', {})
                    
                    # Добавляем отладочный вывод
                    print("Stream Settings:", stream_settings)
                    print("Reality Settings:", reality_settings)
                    
                    # Полчаем publicKey из настроек текущего inbound
                    pbk = reality_settings.get('publicKey', '')
                    if not pbk:  # Если publicKey не найден в realitySettings
                        pbk = stream_settings.get('realitySettings', {}).get('settings', {}).get('publicKey', '')
                    
                    sid = reality_settings.get('shortIds', [''])[0]
                    server_name = reality_settings.get('serverNames', [''])[0]
                    port = inbound.get('port', '')
                    
                    # Формируем базовую часть ссылки
                    link = f"vless://{client_id}@{domain_port}:{port}"
                    
                    # Добавляем параметры в определенном порядке
                    params = []
                    if tcp:
                        params.append(f"type={tcp}")
                    if reality:
                        params.append(f"security={reality}")
                    if pbk:
                        params.append(f"pbk={pbk}")
                    params.append("fp=chrome")
                    if server_name:
                        params.append(f"sni={server_name}")
                    if sid:
                        params.append(f"sid={sid}")
                    params.append("spx=%2F")
                    if client_flow:
                        params.append(f"flow={client_flow}")
                    
                    # Собирае финальную сылку
                    link = f"{link}?{'&'.join(params)}#vless2-{email}"
                    
                    return jsonify({'success': True, 'link': link})
        
        return jsonify({'success': False, 'error': 'Клиент не найде'})
        
    except Exception as e:
        print("Error:", str(e))  # Добавлем вывод ошибки
        return jsonify({'success': False, 'error': str(e)})

@app.template_filter('datetime')
def timestamp_to_datetime(timestamp):
    return datetime.fromtimestamp(timestamp)

# Добавим новую функцию для прверки статуса платежа
def check_payment_status(payment_id):
    """Общая функция для проверки статуса платежа"""
    db = get_db()
    try:
        # Начинаем транзакцию
        db.execute('BEGIN IMMEDIATE')
        
        # Проверяем, не обработан ли уже платеж
        payment = db.execute('SELECT * FROM payments WHERE payment_id = ?', 
                           [payment_id]).fetchone()
        
        if not payment or payment['status'] != 'pending':
            db.rollback()
            return False

        # Получаем настройки YooMoney
        yoomoney_settings = get_yoomoney_settings()
        if not yoomoney_settings:
            db.rollback()
            return False

        # Создаем клиента YooMoney
        client = Client(yoomoney_settings['secret_key'])
        
        # Получаем историю операций
        history = client.operation_history()
        
        # Проверяем каждую операцию
        payment_processed = False
        for operation in history.operations:
            if (hasattr(operation, 'label') and 
                operation.label == payment_id and 
                operation.status == 'success'):
                
                # Обновляем статус платежа
                db.execute('''UPDATE payments 
                             SET status = ?, paid_at = CURRENT_TIMESTAMP 
                             WHERE payment_id = ?''',
                          ['paid', payment_id])
                
                payment_processed = True
                break
        
        if payment_processed:
            # Обновляем дни подписки
            try:
                update_client_expiry(
                    payment['inbound_id'],
                    payment['email'],
                    payment['days']
                )
                
                # Отправляем уведомление в Telegram
                settings = get_telegram_settings()
                if settings and settings['is_enabled']:
                    client_data = db.execute(
                        'SELECT tgid FROM client_data WHERE email = ?',
                        [payment['email']]
                    ).fetchone()
                    
                    if client_data and client_data['tgid']:
                        bot = telebot.TeleBot(settings['bot_token'])
                        message = (
                            f"✅ Оплата получена\n\n"
                            f"Сумма: {payment['amount']} ₽\n"
                            f"Дней добавлено: {payment['days']}\n"
                            f"Спасибо за оплату!"
                        )
                        bot.send_message(client_data['tgid'], message)
                
                db.commit()
                return True
            except Exception as e:
                print(f"Error processing payment: {str(e)}")
                db.rollback()
                return False
        else:
            db.rollback()
            return False
        
    except Exception as e:
        print(f"Error checking payment status: {str(e)}")
        db.rollback()
        return False
    finally:
        if db:
            try:
                db.rollback()  # На всякий случай откатываем незавершенную транзакцию
            except:
                pass

# Обновляем функцию check_pending_payments
def check_pending_payments():
    print("Starting automatic payment check...")
    db = get_db()
    try:
        pending_payments = db.execute(
            'SELECT payment_id FROM payments WHERE status = ?', 
            ['pending']
        ).fetchall()
        
        if not pending_payments:
            print("No pending payments found.")
            return
            
        print(f"Found {len(pending_payments)} pending payments")
        
        for payment in pending_payments:
            try:
                check_payment_status(payment['payment_id'])
            except Exception as e:
                print(f"Error processing payment {payment['payment_id']}: {str(e)}")
                continue
                
    except Exception as e:
        print(f"Error in check_pending_payments: {str(e)}")
    finally:
        db.close()

# Обновляем маршрут для проверки платежа
@app.route('/payments/check', methods=['POST'])
@login_required
def check_payment():
    try:
        data = request.get_json()
        payment_id = data['payment_id']
        
        if check_payment_status(payment_id):
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Платеж не найден или не оплачен'})
        
    except Exception as e:
        print(f"Error checking payment: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/payments/create', methods=['POST'])
@login_required
def create_payment():
    yoomoney_settings = get_yoomoney_settings()
    if not yoomoney_settings or not yoomoney_settings['is_enabled']:
        return jsonify({'success': False, 'error': 'YooMoney не настроен'})
    
    try:
        data = request.get_json()
        email = data['email']
        amount = float(data['amount'])
        days = int(data['days'])
        inbound_id = data['inbound_id']
        tgid = data.get('tgid')

        # Создаем уникальный ID платежа
        payment_id = f"vpn_{email}_{int(datetime.now().timestamp())}"

        # Создам форму оплат
        quickpay = Quickpay(
            receiver=yoomoney_settings['wallet_id'],
            quickpay_form="shop",
            targets=f"Продление VPN для {email} на {days} дней",
            paymentType="SB",
            sum=amount,
            label=payment_id,
            successURL=yoomoney_settings['redirect_url']
        )

        # Сохраняем информацию о платеже
        db = get_db()
        db.execute('''INSERT INTO payments 
                     (email, amount, days, payment_id, inbound_id) 
                     VALUES (?, ?, ?, ?, ?)''',
                  [email, amount, days, payment_id, inbound_id])
        db.commit()
        db.close()

        # Если указан Telegram ID, отправляем ссылку на оплату
        if tgid:
            bot = telebot.TeleBot(get_telegram_settings()['bot_token'])
            message = (
                f"💰 Счет на олату\n\n"
                f"Сумма: {amount} ₽\n"
                f"Дней: {days}\n\n"
                f"Ссылка дя оплаты:\n{quickpay.redirected_url}"
            )
            bot.send_message(tgid, message)

        return jsonify({
            'success': True,
            'payment_url': quickpay.redirected_url
        })

    except Exception as e:
        print(f"Error creating payment: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

def update_client_expiry(inbound_id, email, days):
    settings = get_settings()
    if not settings:
        raise Exception('Настройки панели не найдены')
    
    try:
        session = requests.Session()
        
        # Логин
        login_response = session.post(
            f"{settings['panel_url']}/login",
            data={'username': settings['username'], 'password': settings['password']}
        )
        
        if login_response.status_code != 200:
            raise Exception('Ошибка авторизации')
        
        # олучаем текущие данные inbound
        inbounds_response = session.get(
            f"{settings['panel_url']}/panel/api/inbounds/list",
            headers={'Accept': 'application/json'}
        )
        
        if inbounds_response.status_code != 200:
            raise Exception(f'Ошибка получения данных inbound: {inbounds_response.status_code}')
            
        try:
            inbounds_data = inbounds_response.json()
        except ValueError as e:
            raise Exception(f'Ошибка парсинга ответа: {str(e)}, Ответ: {inbounds_response.text}')
        
        if not inbounds_data.get('success'):
            raise Exception('Ошибка получения днны inbound')
        
        # Ищем нужный inbound и киента
        for inbound in inbounds_data['obj']:
            if str(inbound['id']) == str(inbound_id):
                settings_json = json.loads(inbound['settings'])
                
                # Ищем клиента по email
                for client in settings_json.get('clients', []):
                    if client['email'] == email:
                        # Вычисляем нвую дату окончания
                        current_time = int(datetime.now().timestamp() * 1000)
                        current_expiry = int(client.get('expiryTime', current_time))
                        if current_expiry < current_time:
                            current_expiry = current_time
                            
                        new_expiry = current_expiry + (int(days) * 24 * 60 * 60 * 1000)
                        
                        # Формируем данные дя обновлени  правильном форате
                        update_data = {
                            "id": int(inbound_id),
                            "settings": json.dumps({
                                "clients": [{
                                    "id": client['id'],
                                    "alterId": 0,
                                    "email": email,
                                    "limitIp": client.get('limitIp', 0),
                                    "totalGB": client.get('totalGB', 0),
                                    "expiryTime": new_expiry,
                                    "enable": True,
                                    "tgId": client.get('tgId', ''),
                                    "subId": client.get('subId', '')
                                }]
                            })
                        }
                        
                        # Обновляем данные клиента
                        update_response = session.post(
                            f"{settings['panel_url']}/panel/api/inbounds/updateClient/{client['id']}",
                            json=update_data,
                            headers={'Accept': 'application/json'}
                        )
                        
                        if update_response.status_code != 200:
                            raise Exception(f'Ошибка обновлния данных: {update_response.status_code}')
                            
                        try:
                            response_data = update_response.json()
                        except ValueError as e:
                            raise Exception(f'Ошибка парсинга ответа: {str(e)}, Ответ: {update_response.text}')
                        
                        if not response_data.get('success'):
                            raise Exception('Ошибка обновления данных клиента')
                        
                        print(f"Successfully updated expiry time for {email} to {new_expiry}")
                        return True
                        
        raise Exception(f'Клиент {email} не найден в inbound {inbound_id}')
        
    except Exception as e:
        print(f"Error in update_client_expiry: {str(e)}")
        raise

@app.route('/payments/callback', methods=['POST'])
@login_required
def payment_callback():
    try:
        data = request.get_json()
        label = data.get('label')
        if not label:
            return jsonify({'success': False, 'error': 'Не указан label'})

        db = get_db()
        payment = db.execute('SELECT * FROM payments WHERE payment_id = ?', [label]).fetchone()
        
        if not payment:
            return jsonify({'success': False, 'error': 'Платеж не найден'})

        if payment['status'] != 'pending':
            return jsonify({'success': False, 'error': 'Неверны статус платежа'})

        # Обновляем сатус платежа
        db.execute('''UPDATE payments 
                     SET status = ?, paid_at = CURRENT_TIMESTAMP 
                     WHERE payment_id = ?''',
                  ['paid', label])
        db.commit()

        # Обновляем дни подписки киента
        try:
            update_client_expiry(payment['inbound_id'], payment['email'], payment['days'])
        except Exception as e:
            db.rollback()
            return jsonify({'success': False, 'error': f'Ошиба обновленя подписки: {str(e)}'})

        # тпрвляем уведмление в Telegram
        settings = get_telegram_settings()
        if settings and settings['is_enabled']:
            client = db.execute('SELECT tgid FROM client_data WHERE email = ?', [payment['email']]).fetchone()
            if client and client['tgid']:
                bot = telebot.TeleBot(settings['bot_token'])
                message = (
                    f"✅ Оплата получена\n\n"
                    f"Сумма: {payment['amount']} \n"
                    f"Дней добавлено: {payment['days']}\n"
                    f"Спасибо за оплату!"
                )
                bot.send_message(client['tgid'], message)

        db.close()
        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/payments/cancel', methods=['POST'])
@login_required
def cancel_payment():
    try:
        data = request.get_json()
        payment_id = data['payment_id']

        db = get_db()
        db.execute('UPDATE payments SET status = ? WHERE payment_id = ?',
                  ['cancelled', payment_id])
        db.commit()
        db.close()

        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/payments/delete', methods=['POST'])
@login_required
def delete_payment():
    try:
        data = request.get_json()
        payment_id = data['payment_id']

        db = get_db()
        # Преряем, что платеж существует и имеет статус 'cancelled'
        payment = db.execute('SELECT status FROM payments WHERE payment_id = ?', [payment_id]).fetchone()
        
        if not payment:
            return jsonify({'success': False, 'error': 'Платеж не найден'})
            
        if payment['status'] != 'cancelled':
            return jsonify({'success': False, 'error': 'ожно удалять олько отмененные платежи'})

        # Удаляем платеж
        db.execute('DELETE FROM payments WHERE payment_id = ?', [payment_id])
        db.commit()
        db.close()

        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        panel_url = request.form['panel_url']
        username = request.form['username']
        password = request.form['password']
        
        db = get_db()
        db.execute('''INSERT INTO settings 
                     (panel_url, username, password) 
                     VALUES (?, ?, ?)''',
                  [panel_url, username, password])
        db.commit()
        db.close()
        
        flash('Нстройки панели упешно охранены')
        return redirect(url_for('settings'))
    
    settings = get_settings()
    return render_template('settings.html', settings=settings)

# Добавим функцию для получения сообщений бота
def get_bot_message(message_type):
    try:
        with app.app_context():
            db = get_db()
            try:
                message = db.execute(
                    'SELECT message_text FROM bot_messages WHERE message_type = ?', 
                    [message_type]
                ).fetchone()
                
                return message['message_text'] if message else None
                
            finally:
                db.close()
    except Exception as e:
        print(f"Error getting bot message: {str(e)}")
        return None

# Добави функцию для обработки комады /stat
def handle_stat_command(message, telegram_bot):
    try:
        tgid = str(message.chat.id)
        print(f"Received /stat command from tgid: {tgid}")
        
        with app.app_context():
            db = get_db()
            try:
                client_data = db.execute(
                    'SELECT email FROM client_data WHERE tgid = ? COLLATE NOCASE',
                    [tgid]
                ).fetchone()
                
                if not client_data:
                    print(f"No client found for tgid: {tgid}")
                    
                    # Проверяем, включена ли выдача тестовых аккаунтов
                    test_settings = db.execute(
                        'SELECT * FROM test_account_settings WHERE id = 1'
                    ).fetchone()
                    
                    if test_settings and test_settings['is_enabled']:
                        # Создаем тестовый аккаунт
                        try:
                            # Генерируем email для тестового аккаунта
                            test_email = f"{tgid}@vpn.syslab.space"
                            
                            # олучаем настройки панели
                            panel_settings = get_settings()
                            
                            # Поучаем первый оступный inbound
                            session = requests.Session()
                            login_response = session.post(
                                f"{panel_settings['panel_url']}/login",
                                data={'username': panel_settings['username'], 
                                      'password': panel_settings['password']}
                            )
                            
                            if login_response.status_code != 200:
                                raise Exception("Ошибка авторизации в панели")
                            
                            inbounds_response = session.get(
                                f"{panel_settings['panel_url']}/panel/api/inbounds/list"
                            )
                            inbounds_data = inbounds_response.json()
                            
                            if not inbounds_data['success'] or not inbounds_data['obj']:
                                raise Exception("Не найдены доступны inbound")
                            
                            inbound_id = inbounds_data['obj'][0]['id']
                            
                            # Создаем клиента в панели
                            client_id = str(uuid.uuid4())
                            expiry_time = int(time.time() * 1000) + (test_settings['days'] * 24 * 60 * 60 * 1000)
                            traffic_limit = test_settings['traffic_gb'] * 1024 * 1024 * 1024
                            
                            client_data = {
                                "id": inbound_id,
                                "settings": json.dumps({
                                    "clients": [{
                                        "id": client_id,
                                        "flow": "",
                                        "email": test_email,
                                        "limitIp": 0,
                                        "totalGB": traffic_limit,
                                        "expiryTime": expiry_time,
                                        "enable": True,
                                        "tgId": tgid,
                                        "subId": client_id
                                    }]
                                })
                            }
                            
                            # Добавляем клиента в пнель
                            add_response = session.post(
                                f"{panel_settings['panel_url']}/panel/api/inbounds/addClient",
                                json=client_data
                            )
                            
                            if not add_response.json()['success']:
                                raise Exception("Ошибка создания тестового аккаунта")
                            
                            # Сохраняем данные в локальной базе
                            db.execute(
                                'INSERT INTO client_data (email, tgid) VALUES (?, ?)',
                                [test_email, tgid]
                            )
                            db.commit()
                            
                            # Отправляем сообщение с данными тестового аккаунта
                            message_text = (
                                f"✅ Вам создан тестовый аккаунт!\n\n"
                                f"Email: {test_email}\n"
                                f"Срок действия: {test_settings['days']} дней\n"
                                f"Лимит трафика: {'∞' if test_settings['traffic_gb'] == 0 else str(test_settings['traffic_gb']) + ' GB'}\n\n"
                                f"Испольуйте команд /stat для получения ссылки подключения."
                            )
                            telegram_bot.send_message(message.chat.id, message_text)
                            return
                            
                        except Exception as e:
                            print(f"Error creating test account: {str(e)}")
                            error_message = get_bot_message('tgid_not_found')
                            if not error_message:
                                error_message = "Ваш Telegram ID не найден в базе. Обратитесь к администратору."
                            telegram_bot.send_message(message.chat.id, error_message)
                            return
                    else:
                        error_message = get_bot_message('tgid_not_found')
                        if not error_message:
                            error_message = "Ваш Telegram ID не найден в базе. Обртитесь к администратору."
                        telegram_bot.send_message(message.chat.id, error_message)
                        return
                else:
                    # Получаем данные клиента из панели
                    panel_settings = get_settings()
                    session = requests.Session()
                    
                    # Логин
                    login_response = session.post(
                        f"{panel_settings['panel_url']}/login",
                        data={'username': panel_settings['username'], 'password': panel_settings['password']}
                    )
                    
                    if login_response.status_code != 200:
                        telegram_bot.send_message(message.chat.id, "Ошибка получения данных")
                        return
                    
                    # Получаем список клиентов
                    clients_response = session.get(f"{panel_settings['panel_url']}/panel/api/inbounds/list")
                    clients_data = clients_response.json()
                    
                    if not clients_data['success']:
                        telegram_bot.send_message(message.chat.id, "Ошибка получения данных")
                        return
                    
                    # Ищем клиента по email
                    email = client_data['email']
                    client_found = False
                    
                    for inbound in clients_data['obj']:
                        if client_found:
                            break
                        
                        for client in inbound['clientStats']:
                            if client['email'] == email:
                                client_found = True
                                # Проверяем срок действия подписки
                                if client['expiryTime'] and client['expiryTime'] != '0':
                                    current_time = datetime.now().timestamp() * 1000
                                    time_left = float(client['expiryTime']) - current_time
                                    
                                    if time_left <= 0:
                                        # Получаем настройки для создания платежа
                                        settings = get_telegram_settings()
                                        if settings and 'payment_amount' in dict(settings):  # Изменено здесь
                                            # Создаем клавиатуру с кнопками
                                            markup = telebot.types.InlineKeyboardMarkup()
                                            markup.row(
                                                telebot.types.InlineKeyboardButton("Да", callback_data=f"create_payment:{email}:{inbound['id']}"),
                                                telebot.types.InlineKeyboardButton("Нет", callback_data="reject_payment")
                                            )
                                            
                                            # Формируем сообщение
                                            message_text = (
                                                f"📊 Статистика пользователя: {email}\n\n"
                                                f"⚠️ Подписка недействительна (срок действия истек)\n\n"
                                                "Созать счет для продления?"
                                            )
                                            
                                            telegram_bot.send_message(message.chat.id, message_text, reply_markup=markup)
                                            return
                                
                                # Формируем обычное сообщение со статистикой
                                traffic_up = float(client['up']) / (1024 * 1024 * 1024)
                                traffic_down = float(client['down']) / (1024 * 1024 * 1024)
                                
                                message_text = (
                                    f"📊 Статистика пользователя: {email}\n\n"
                                    f"📤 Отправлено: {traffic_up:.2f} GB\n"
                                    f"📥 Скачано: {traffic_down:.2f} GB\n"
                                )
                                
                                if client['total'] > 0:
                                    total_gb = float(client['total']) / (1024 * 1024 * 1024)
                                    message_text += f"💾 Лимит трфка: {total_gb:.2f} GB\n"
                                else:
                                    message_text += "💾 Лимит трафика: ∞\n"
                                
                                if client['expiryTime'] and client['expiryTime'] != '0':
                                    current_time = datetime.now().timestamp() * 1000
                                    time_left = float(client['expiryTime']) - current_time
                                    
                                    if time_left <= 0:
                                        message_text += "⏳ Подписка недействительна (срок действия истек)\n\n"
                                    else:
                                        days_left = int(time_left / (1000 * 60 * 60 * 24))
                                        hours_left = int((time_left % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60))
                                        
                                        if days_left > 0:
                                            message_text += f"⏳ До окончания подписки: {days_left} дн. {hours_left} ч.\n\n"
                                        elif hours_left > 0:
                                            message_text += f"⏳ До окончания подписки: {hours_left} ч.\n\n"
                                else:
                                    message_text += "⏳ Срок действия: бессрочно\n\n"

                                # Добавляем получение ссылки для подключения
                                settings_json = json.loads(inbound['settings'])
                                stream_settings = json.loads(inbound['streamSettings'])
                                
                                # Извлекаем домен из URL панели
                                panel_url = panel_settings['panel_url']
                                domain_part = panel_url.split('://')[-1]
                                domain = domain_part.split('/')[0].split(':')[0]
                                
                                # Ищем клиента по email для получения ID и flow
                                client_id = None
                                client_flow = None
                                for c in settings_json.get('clients', []):
                                    if c['email'] == email:
                                        client_id = c['id']
                                        client_flow = c.get('flow', 'xtls-rprx-vision')
                                        break
                                
                                if client_id:
                                    # Полуаем параметры для ссылки
                                    tcp = stream_settings.get('network', '')
                                    reality = stream_settings.get('security', '')
                                    reality_settings = stream_settings.get('realitySettings', {})
                                    
                                    pbk = reality_settings.get('publicKey', '')
                                    if not pbk:
                                        pbk = stream_settings.get('realitySettings', {}).get('settings', {}).get('publicKey', '')
                                    
                                    sid = reality_settings.get('shortIds', [''])[0]
                                    server_name = reality_settings.get('serverNames', [''])[0]
                                    port = inbound.get('port', '')
                                    
                                    # Формируем ссылку
                                    params = [
                                        f"type={tcp}",
                                        f"security={reality}",
                                        f"pbk={pbk}",
                                        "fp=chrome",
                                        f"sni={server_name}",
                                        f"sid={sid}",
                                        "spx=%2F"
                                    ]
                                    
                                    if client_flow:
                                        params.append(f"flow={client_flow}")
                                    
                                    link = f"vless://{client_id}@{domain}:{port}?{'&'.join(params)}#vless2-{email}"
                                    
                                    # Добавляем ссылку в сообщение
                                    message_text += f"🔗 Ссылка для подключения:\n<code>{link}</code>"
                                
                                # Отправляем сообщение с поддержкой HTML
                                telegram_bot.send_message(message.chat.id, message_text, parse_mode='HTML')
                                break
                    
                    if not client_found:
                        print(f"Client stats not found for email: {email}")
                        telegram_bot.send_message(message.chat.id, "Ошибка 5555")
                        
                        # Получаем настройки для создания платежа
                        settings = get_telegram_settings()
                        if settings and 'payment_amount' in dict(settings):
                            # Создаем клавиатуру с кнопками
                            markup = telebot.types.InlineKeyboardMarkup()
                            markup.row(
                                telebot.types.InlineKeyboardButton("Да", callback_data=f"create_payment:{email}:{inbound['id']}"),
                                telebot.types.InlineKeyboardButton("Нет", callback_data="reject_payment")
                            )
                            
                            # Отправляем сообщение с кнопками
                            telegram_bot.send_message(
                                message.chat.id, 
                                f"{message_text}\nСоздать счет для продления?", 
                                parse_mode='HTML',
                                reply_markup=markup
                            )
                            return
                        
            finally:
                db.close()
                
    except Exception as e:
        print(f"Error in /stat command: {str(e)}")
        telegram_bot.send_message(message.chat.id, "Произошла ошибка при получении статистики")

def handle_telegram_commands():
    global telegram_bot, bot_thread, bot_running
    settings = get_telegram_settings()
    if not settings or not settings['is_enabled']:
        return
    
    try:
        # Останавливаем предыдущий экземпляр бота
        if telegram_bot is not None:
            print("Stopping previous bot instance...")
            bot_running = False
            telegram_bot.stop_polling()
            if bot_thread and bot_thread.is_alive():
                bot_thread.join(timeout=5)
            telegram_bot = None
            bot_thread = None
            print("Previous bot instance stopped")
        
        # Создаем новый экземпляр бота
        telegram_bot = telebot.TeleBot(settings['bot_token'])
        print("Created new bot instance")
        
        # Очищаем все обработчики еред добавлением новых
        telegram_bot.message_handlers = []
        telegram_bot.callback_query_handlers = []
        
        # Регистрируем обработчики команд
        @telegram_bot.message_handler(commands=['start'])
        def send_welcome(message):
            try:
                with app.app_context():
                    db = get_db()
                    try:
                        start_message = db.execute(
                            'SELECT message_text, image_path, show_image FROM bot_messages WHERE message_type = ?',
                            ['start_message']
                        ).fetchone()
                        
                        if not start_message:
                            start_message = {
                                'message_text': 'Добро пожаловать! Используйте команду /stat для получения статистики вашего аккаунта.',
                                'image_path': None,
                                'show_image': False
                            }
                        
                        if start_message['image_path'] and start_message['show_image']:
                            # Отправляем фото с подписью
                            try:
                                with open(os.path.join('static', start_message['image_path']), 'rb') as photo:
                                    telegram_bot.send_photo(
                                        message.chat.id,
                                        photo,
                                        caption=start_message['message_text'],
                                        parse_mode='HTML'
                                    )
                            except Exception as e:
                                print(f"Error sending photo: {str(e)}")
                                telegram_bot.send_message(
                                    message.chat.id, 
                                    start_message['message_text'],
                                    parse_mode='HTML'
                                )
                        else:
                            # Отправляем только текст
                            telegram_bot.send_message(
                                message.chat.id, 
                                start_message['message_text'],
                                parse_mode='HTML'
                            )
                    finally:
                        db.close()
            except Exception as e:
                print(f"Error in /start command: {str(e)}")
            
        @telegram_bot.message_handler(commands=['stat'])
        def send_stats(message):
            handle_stat_command(message, telegram_bot)
            
        @telegram_bot.message_handler(commands=['info'])
        def send_info(message):
            try:
                with app.app_context():
                    db = get_db()
                    try:
                        info_message = db.execute(
                            'SELECT message_text, is_enabled FROM bot_messages WHERE message_type = ?',
                            ['info_message']
                        ).fetchone()
                        
                        if info_message and info_message['is_enabled']:
                            telegram_bot.send_message(
                                message.chat.id, 
                                info_message['message_text'],
                                parse_mode='HTML'
                            )
                    finally:
                        db.close()
            except Exception as e:
                print(f"Error in /info command: {str(e)}")
        
        # Добавляем обработчик callback-кнопок
        @telegram_bot.callback_query_handler(func=lambda call: True)
        def handle_callback(call):
            try:
                if call.data == "reject_payment":
                    # Удаляем сообщение с кнопками
                    telegram_bot.delete_message(call.message.chat.id, call.message.message_id)
                    telegram_bot.answer_callback_query(call.id, "Операция отменена")
                    return

                if call.data.startswith("create_payment:"):
                    # Разбираем данные из callback
                    _, email, inbound_id = call.data.split(":")
                    
                    with app.app_context():
                        # Получаем настройки
                        settings = get_telegram_settings()
                        if not settings or not settings.get('payment_amount'):
                            telegram_bot.answer_callback_query(call.id, "Ошибка: не настроена сумма платежа")
                            return

                        try:
                            # Создаем платеж
                            payment = create_payment_for_client(
                                email=email,
                                amount=float(settings['payment_amount']),
                                days=30,  # стандартный период
                                inbound_id=inbound_id,
                                tgid=str(call.message.chat.id)
                            )

                            if payment.get('success'):
                                # Удаляем сообщение с кнопками
                                telegram_bot.delete_message(call.message.chat.id, call.message.message_id)
                                # Отправляем новое сообщение со ссылкой на оплату
                                message = (
                                    f"💰 Создан счет на оплату\n\n"
                                    f"Сумма: {settings['payment_amount']} ₽\n"
                                    f"Дней: 30\n\n"
                                    f"Ссылка для оплаты:\n{payment['payment_url']}"
                                )
                                telegram_bot.send_message(call.message.chat.id, message)
                                telegram_bot.answer_callback_query(call.id, "Счет создан")
                            else:
                                telegram_bot.answer_callback_query(
                                    call.id, 
                                    f"Ошибка создания платежа: {payment.get('error', 'Неизвестная ошибка')}"
                                )
                        except Exception as e:
                            print(f"Error creating payment: {str(e)}")
                            telegram_bot.answer_callback_query(
                                call.id, 
                                f"Ошибка создания платежа: {str(e)}"
                            )
            except Exception as e:
                print(f"Error in callback handler: {str(e)}")
                try:
                    telegram_bot.answer_callback_query(call.id, "Произошла ошибка")
                except:
                    pass

        # Запускаем бота в отдельном потоке
        def run_bot():
            global bot_running
            bot_running = True
            print("Starting bot polling...")
            while bot_running:
                try:
                    telegram_bot.polling(none_stop=True, interval=3, timeout=30)
                except Exception as e:
                    print(f"Bot polling error: {str(e)}")
                    if not bot_running:
                        break
                    time.sleep(5)
            print("Bot polling stopped")
        
        if bot_thread and bot_thread.is_alive():
            bot_thread.join(timeout=5)
        
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        print("Bot thread started")
        
    except Exception as e:
        print(f"Error starting Telegram bot: {str(e)}")

def stop_telegram_bot():
    global telegram_bot, bot_thread, bot_running
    try:
        if telegram_bot is not None:
            print("Stopping Telegram bot...")
            bot_running = False  # Сигнал для остановки бота
            telegram_bot.stop_polling()
            if bot_thread and bot_thread.is_alive():
                bot_thread.join(timeout=5)
            telegram_bot = None
            bot_thread = None
            print("Telegram bot stopped")
    except Exception as e:
        print(f"Error in stop_telegram_bot: {str(e)}")

def restart_scheduler():
    """Функци для перезапуска планировщика с новыми настройками"""
    global scheduler
    
    try:
        if scheduler and scheduler.running:
            print("Stopping current scheduler...")
            scheduler.shutdown()
            scheduler = None
            print("Scheduler stopped")
        
        print("Starting new scheduler...")
        init_scheduler()
        print("Scheduler restarted successfully")
        
    except Exception as e:
        print(f"Error restarting scheduler: {str(e)}")
        # Если произошла ошибка, пытаемся создать новый планировщик
        scheduler = None
        init_scheduler()

# Переместим маршрут bot_messages после определения всех необходимых функций
@app.route('/bot_messages', methods=['GET', 'POST'])
@login_required
def bot_messages():
    if request.method == 'POST':
        form_type = request.form.get('form_type')
        
        db = get_db()
        try:
            if form_type == 'error_message':
                tgid_not_found = request.form['tgid_not_found']
                create_test_account = 'create_test_account' in request.form
                
                # Обовляем сообщение об ошибке
                existing = db.execute('SELECT id FROM bot_messages WHERE message_type = ?', 
                                    ['tgid_not_found']).fetchone()
                if existing:
                    db.execute('UPDATE bot_messages SET message_text = ? WHERE message_type = ?',
                             [tgid_not_found, 'tgid_not_found'])
                else:
                    db.execute('INSERT INTO bot_messages (message_type, message_text) VALUES (?, ?)',
                             ['tgid_not_found', tgid_not_found])
                
                # Обновляем настройку выачи тестового аккаунта
                db.execute('UPDATE test_account_settings SET is_enabled = ? WHERE id = 1',
                          [1 if create_test_account else 0])
                
            elif form_type == 'start_message':
                start_message = request.form['start_message']
                remove_image = 'remove_image' in request.form
                show_image = 1 if 'show_image' in request.form else 0  # Преобразуем в 1 или 0 для SQLite
                
                # Получаем текущий путь к изображению
                current_image = db.execute(
                    'SELECT image_path FROM bot_messages WHERE message_type = ?',
                    ['start_message']
                ).fetchone()
                
                new_image_path = None
                if 'start_image' in request.files:
                    file = request.files['start_image']
                    if file and file.filename and allowed_file(file.filename):
                        # Удаляем старое изображение если оно есть
                        if current_image and current_image['image_path']:
                            old_image_path = os.path.join('static', current_image['image_path'])
                            if os.path.exists(old_image_path):
                                os.remove(old_image_path)
                        
                        # Сохраняем новое изображение
                        filename = secure_filename(file.filename)
                        timestamp = int(datetime.now().timestamp())
                        new_filename = f"{timestamp}_{filename}"
                        file.save(os.path.join(UPLOAD_FOLDER, new_filename))
                        new_image_path = f"uploads/{new_filename}"
                
                # Если выбрано удаление изображения
                if remove_image and current_image and current_image['image_path']:
                    old_image_path = os.path.join('static', current_image['image_path'])
                    if os.path.exists(old_image_path):
                        os.remove(old_image_path)
                    new_image_path = None
                
                # Обновляем или создаем запись
                existing = db.execute('SELECT id FROM bot_messages WHERE message_type = ?', 
                                    ['start_message']).fetchone()
                if existing:
                    if new_image_path is not None:
                        db.execute('''UPDATE bot_messages 
                                    SET message_text = ?, image_path = ?, show_image = ? 
                                    WHERE message_type = ?''',
                                 [start_message, new_image_path, show_image, 'start_message'])
                    elif remove_image:
                        db.execute('''UPDATE bot_messages 
                                    SET message_text = ?, image_path = NULL, show_image = ? 
                                    WHERE message_type = ?''',
                                 [start_message, show_image, 'start_message'])
                    else:
                        db.execute('''UPDATE bot_messages 
                                    SET message_text = ?, show_image = ? 
                                    WHERE message_type = ?''',
                                 [start_message, show_image, 'start_message'])
                else:
                    db.execute('''INSERT INTO bot_messages 
                                (message_type, message_text, image_path, show_image) 
                                VALUES (?, ?, ?, ?)''',
                             ['start_message', start_message, new_image_path, show_image])
            
            elif form_type == 'test_account':
                test_days = int(request.form['test_days'])
                test_traffic = int(request.form['test_traffic'])
                
                # Проверяем существование записи
                existing = db.execute('SELECT id FROM test_account_settings WHERE id = 1').fetchone()
                if existing:
                    # Обновляем существующую запись
                    db.execute('''UPDATE test_account_settings 
                                 SET days = ?, traffic_gb = ? 
                                 WHERE id = 1''',
                              [test_days, test_traffic])
                else:
                    # Создаем новую запись
                    db.execute('''INSERT INTO test_account_settings 
                                 (id, days, traffic_gb) 
                                 VALUES (1, ?, ?)''',
                              [test_days, test_traffic])
            
            elif form_type == 'info_message':
                info_message = request.form['info_message']
                info_enabled = 'info_enabled' in request.form
                
                existing = db.execute('SELECT id FROM bot_messages WHERE message_type = ?', 
                                         ['info_message']).fetchone()
                if existing:
                    db.execute('UPDATE bot_messages SET message_text = ?, is_enabled = ? WHERE message_type = ?',
                              [info_message, info_enabled, 'info_message'])
                else:
                    db.execute('INSERT INTO bot_messages (message_type, message_text, is_enabled) VALUES (?, ?, ?)',
                              ['info_message', info_message, info_enabled])
            
            db.commit()
            flash('Настройки успешно схранены')
            
            # Перезапускаем бота для применения новых настроек
            handle_telegram_commands()
            
        except Exception as e:
            db.rollback()
            flash(f'Ошибка при сохранении: {str(e)}', 'error')
        finally:
            db.close()
        
        return redirect(url_for('bot_messages'))
    
    # Получаем все настройки из базы данных
    db = get_db()
    try:
        messages = {}
        # Получаем текст, состояние is_enabled и show_image для каждого сообщения
        for row in db.execute('SELECT message_type, message_text, is_enabled, image_path, show_image FROM bot_messages'):
            messages[row['message_type']] = {
                'text': row['message_text'],
                'is_enabled': row['is_enabled'],
                'image_path': row['image_path'],
                'show_image': bool(row['show_image'])  # Преобразуем в булево значение
            }
        
        test_settings = db.execute('SELECT * FROM test_account_settings WHERE id = 1').fetchone()
            
    finally:
        db.close()
    
    return render_template('bot_messages.html', 
                         messages=messages,
                         test_settings=test_settings)

def init_scheduler():
    """Инициализация планировщика задач"""
    global scheduler
    
    try:
        # Останавливаем существующий планировщик если он запущен
        if scheduler and scheduler.running:
            print("Stopping current scheduler...")
            scheduler.shutdown()
            scheduler = None
            print("Scheduler stopped")
        
        # Создем новый планировщик
        scheduler = BackgroundScheduler(
            timezone=utc,
            job_defaults={
                'coalesce': False,
                'max_instances': 1
            }
        )
        
        # Получаем настройки интервала для проверки подписок
        with app.app_context():
            settings = get_telegram_settings()
            if settings and settings['check_interval']:
                interval = int(settings['check_interval'])
                if settings['interval_unit'] == 'hours':
                    interval *= 60  # Конвертируем часы в минуты
            else:
                interval = 60  # По умолчанию 60 минут
        
        # Добавляем задачу проверки подписок
        scheduler.add_job(
            func=check_expiring_subscriptions,
            trigger='interval',
            minutes=interval,
            id='check_subscriptions'
        )
        
        # Добавляем задачу проверки платежей
        scheduler.add_job(
            func=check_pending_payments,
            trigger='interval',
            minutes=1,
            id='check_payments'
        )
        
        scheduler.start()
        print(f"Scheduler started. Checking subscriptions every {interval} minutes and payments every minute.")
        
        return True
        
    except Exception as e:
        print(f"Error initializing scheduler: {str(e)}")
        if scheduler and scheduler.running:
            scheduler.shutdown()
            scheduler = None
        return False

def check_expiring_subscriptions():
    print("Starting subscription check...")  # Добавим отладочный вывод
    try:
        # Создаем новый контекст приложения
        with app.app_context():
            # Получаем настройки из базы данных
            db = get_db()
            try:
                settings = db.execute('SELECT * FROM telegram_settings ORDER BY id DESC LIMIT 1').fetchone()
                panel_settings = db.execute('SELECT * FROM settings ORDER BY id DESC LIMIT 1').fetchone()
                
                if not settings or not settings['is_enabled']:
                    print("Telegram bot is not enabled or settings not found")
                    return
                
                if not panel_settings:
                    print("Panel settings not found")
                    return
                
                print(f"Starting subscription check with notify_days={settings['notify_days']}")
                
                # Очищаем старые уведомления (старше 24 часов)
                db.execute('''DELETE FROM notification_history 
                            WHERE created_at < datetime('now', '-1 day')''')
                db.commit()
                
                # Получаем список клиентов через API панели
                session = requests.Session()
                login_response = session.post(
                    f"{panel_settings['panel_url']}/login",
                    data={'username': panel_settings['username'], 'password': panel_settings['password']}
                )
                
                if login_response.status_code != 200:
                    print("Ошибка авторизации в панели")
                    return
                    
                clients_response = session.get(f"{panel_settings['panel_url']}/panel/api/inbounds/list")
                clients_data = clients_response.json()
                
                if not clients_data.get('success'):
                    print("Ошибка получения списка клиентов")
                    return
                    
                current_time = datetime.now().timestamp() * 1000
                notify_days = int(settings['notify_days'])
                
                print(f"Checking {len(clients_data['obj'])} inbounds...")
                
                # Проверяем каждого клиента
                for inbound in clients_data['obj']:
                    for client in inbound['clientStats']:
                        if client['expiryTime'] > 0:  # Пропускаем бессрочные подписки
                            days_left = (client['expiryTime'] - current_time) / (1000 * 60 * 60 * 24)
                            
                            print(f"Checking client {client['email']}, days left: {days_left:.1f}")
                            
                            # Если осталось меньше указанного количества дней
                            if 0 < days_left < notify_days:
                                # Проверяем, не отправляли ли мы уже уведомление
                                notification = db.execute(
                                    'SELECT * FROM notification_history WHERE email = ? AND expiry_time = ?', 
                                    (client['email'], client['expiryTime'])
                                ).fetchone()
                                
                                if not notification:  # Если уведомление еще не отправлялось
                                    client_data = db.execute(
                                        'SELECT tgid FROM client_data WHERE email = ?',
                                        [client['email']]
                                    ).fetchone()
                                    
                                    if client_data and client_data['tgid']:
                                        print(f"Sending notification to {client['email']}")
                                        
                                        # Создаем плате если включено
                                        payment_link = ''
                                        if settings['create_payment'] and settings['payment_amount']:
                                            try:
                                                payment = create_payment_for_client(
                                                    client['email'],
                                                    float(settings['payment_amount']),
                                                    30,  # Станартный период продления
                                                    inbound['id'],
                                                    client_data['tgid']
                                                )
                                                if payment.get('success'):
                                                    payment_link = payment['payment_url']
                                            except Exception as e:
                                                print(f"Ошибка создания платеа: {str(e)}")
                                        
                                        # Формируем и отправляем уведомление
                                        message = settings['notification_template'].format(
                                            days=int(days_left),
                                            email=client['email'],
                                            payment_link=f"\nСсылка для оплаты:\n{payment_link}" if payment_link else ''
                                        )
                                        
                                        bot = telebot.TeleBot(settings['bot_token'])
                                        bot.send_message(client_data['tgid'], message)
                                        print(f"Notification sent to {client['email']}")
                                        
                                        # Сохраняем информаци об отпраленном уведомлении
                                        db.execute('''
                                            INSERT INTO notification_history (email, expiry_time)
                                            VALUES (?, ?)
                                        ''', [client['email'], client['expiryTime']])
                                        db.commit()
                                    else:
                                        print(f"No Telegram ID found for {client['email']}")
                                else:
                                    print(f"Notification already sent to {client['email']}")
                
                print("Subscription check completed")
                
            except Exception as e:
                print(f"Database error: {str(e)}")
            finally:
                db.close()
    except Exception as e:
        print(f"Error in check_expiring_subscriptions: {str(e)}")

@app.route('/payments')
@login_required
def payments():
    db = get_db()
    try:
        # Получаем все платежи
        payments_raw = db.execute('''
            SELECT * FROM payments 
            ORDER BY 
                CASE status 
                    WHEN 'pending' THEN 1 
                    WHEN 'paid' THEN 2 
                    ELSE 3 
                END,
                created_at DESC
        ''').fetchall()
        
        # Преобразуем даты из строк в объекты datetime
        payments = []
        for payment in payments_raw:
            payment_dict = dict(payment)
            if payment_dict['created_at']:
                payment_dict['created_at'] = datetime.strptime(payment_dict['created_at'], '%Y-%m-%d %H:%M:%S')
            if payment_dict['paid_at']:
                payment_dict['paid_at'] = datetime.strptime(payment_dict['paid_at'], '%Y-%m-%d %H:%M:%S')
            payments.append(payment_dict)
            
        return render_template('payments.html', payments=payments)
    finally:
        db.close()

@app.route('/database', methods=['GET', 'POST'])
@login_required
def database():
    db = get_db()
    try:
        # Получаем список всех таблиц
        tables = db.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' 
            ORDER BY name
        """).fetchall()
        
        tables_data = {}
        for table in tables:
            table_name = table['name']
            # Получаем данные таблицы
            rows = db.execute(f'SELECT * FROM {table_name}').fetchall()
            # Получаем информацию о колонках
            columns = db.execute(f'PRAGMA table_info({table_name})').fetchall()
            column_names = [col['name'] for col in columns]
            tables_data[table_name] = {
                'columns': column_names,
                'rows': [dict(row) for row in rows]
            }
        
        return render_template('database.html', tables_data=tables_data)
    finally:
        db.close()

@app.route('/database/delete', methods=['POST'])
@login_required
def delete_record():
    try:
        data = request.get_json()
        table = data['table']
        record_id = data['id']
        
        db = get_db()
        db.execute(f'DELETE FROM {table} WHERE id = ?', [record_id])
        db.commit()
        db.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# Перемещаем маршрут перед блоком запуска
@app.route('/html_reference')
@login_required
def html_reference():
    return render_template('html_reference.html')

# Основной блок запуска
if __name__ == '__main__':
    shutdown_event = False  # Флаг для отслеживания процесса завершения
    
    def signal_handler(signum, frame):
        global shutdown_event
        if not shutdown_event:  # Проверяем, не начато ли уже завершение
            shutdown_event = True
            print("\nReceived shutdown signal...")
            stop_telegram_bot()
            sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        init_db()
        init_scheduler()
        handle_telegram_commands()
        app.run(debug=True)
    finally:
        if not shutdown_event:  # Останавливаем бота только если еще не остановлен
            stop_telegram_bot()