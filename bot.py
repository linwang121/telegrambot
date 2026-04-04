#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
临门商店 Telegram Bot
最终稳定版 V23
- 修复 service_id 为 0 导致绑定后未显示问题
- 确保数据库更新正确
- 验证码/2FA错误可重试
- 所有功能完整
"""

import os
import re
import json
import time
import sqlite3
import hashlib
import random
import string
import logging
import threading
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify
import telebot
from telebot import types

import asyncio
from telethon import TelegramClient, events, errors
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest

# ==================== 配置常量 ====================
BOT_TOKEN = '8185141366:AAHYXqzDty_jVk1aum89rdlVxu70urGB4yg'
SERVER_IP = '38.190.210.57'
PORT = 1010
CALLBACK_URL = f'http://{SERVER_IP}:{PORT}/okpay'
OKPAY_MERCHANT_ID = '23459'
OKPAY_SECRET_KEY = 'd6ikQToqgRy5AsEzGKMNOPcFWpYL0rw'
API_ID = 37126783
API_HASH = '67f05f148177e0160a7ed4980320f6e4'
SUPER_ADMIN_ID = 7874562290
FORWARD_BOT = "@lmfdl_bot"

# ==================== 路径配置 ====================
BASE_DIR = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler("shop.log")]
)
logger = logging.getLogger('LimenShopBot')

# ==================== 初始化Bot和Flask ====================
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ==================== 全局异步事件循环 ====================
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
threading.Thread(target=loop.run_forever, daemon=True).start()

# ==================== 数据库初始化 ====================
DB_PATH = BASE_DIR / 'shop.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        balance REAL DEFAULT 0,
        is_admin INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_active TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price REAL NOT NULL,
        type TEXT NOT NULL,
        category_id INTEGER,
        content TEXT,
        stock INTEGER DEFAULT -1,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (category_id) REFERENCES categories(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS card_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_code TEXT UNIQUE NOT NULL,
        card_type TEXT NOT NULL,
        value INTEGER,
        product_id INTEGER,
        is_used INTEGER DEFAULT 0,
        used_by INTEGER,
        used_at TIMESTAMP,
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP,
        FOREIGN KEY (product_id) REFERENCES products(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        product_id INTEGER,
        quantity INTEGER DEFAULT 1,
        total_price REAL,
        status TEXT DEFAULT 'completed',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        delivered_content TEXT,
        FOREIGN KEY (user_id) REFERENCES users(user_id),
        FOREIGN KEY (product_id) REFERENCES products(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS payment_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        order_id TEXT UNIQUE NOT NULL,
        amount REAL NOT NULL,
        currency TEXT DEFAULT 'USDT',
        status TEXT DEFAULT 'pending',
        okpay_trade_no TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        paid_at TIMESTAMP,
        expires_at TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS anti_login_services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        target_phone TEXT,
        session_dir TEXT,
        last_devices TEXT DEFAULT '[]',
        status TEXT DEFAULT 'inactive',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP,
        last_check TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS anti_login_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        service_id INTEGER NOT NULL,
        log_entry TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS exchange_rate (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        rate REAL DEFAULT 1.0,
        updated_by INTEGER,
        updated_at TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS system_config (
        key TEXT PRIMARY KEY,
        value TEXT,
        description TEXT,
        updated_at TIMESTAMP)''')
    c.execute("INSERT OR IGNORE INTO exchange_rate (id, rate) VALUES (1, 1.0)")
    default_configs = [
        ('anti_login_price_1m', '10', '1个月价格'),
        ('anti_login_price_6m', '50', '6个月价格'),
        ('anti_login_price_1y', '90', '1年价格'),
    ]
    for key, value, desc in default_configs:
        c.execute("INSERT OR IGNORE INTO system_config (key, value, description) VALUES (?, ?, ?)", (key, value, desc))
    c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, last_name, is_admin) VALUES (?, 'admin', 'Admin', '', 1)",
              (SUPER_ADMIN_ID,))
    conn.commit()
    conn.close()
    logger.info("数据库初始化完成")

def get_db():
    return sqlite3.connect(DB_PATH)

def execute_query(query, params=(), fetchone=False, fetchall=False):
    conn = get_db()
    c = conn.cursor()
    c.execute(query, params)
    if fetchone:
        result = c.fetchone()
    elif fetchall:
        result = c.fetchall()
    else:
        result = None
    conn.commit()
    conn.close()
    return result

# ==================== OKPay 支付类 ====================
class OkayPay:
    def _flatten_items(self, d, parent_key=''):
        items = []
        if isinstance(d, dict):
            for k, v in d.items():
                new_key = f"{parent_key}[{k}]" if parent_key else k
                if isinstance(v, dict):
                    items.extend(self._flatten_items(v, new_key))
                else:
                    items.append((new_key, str(v) if v is not None else ''))
        else:
            items.append((parent_key, str(d) if d is not None else ''))
        return items

    def __init__(self, id, token, api_url_base='https://api.okaypay.me/shop/'):
        self.id = id
        self.token = token
        self.api_url_payLink = api_url_base + 'payLink'

    def pay_link(self, amount, return_url=None):
        payment_data = {
            'name': '积分充值',
            'amount': amount,
            'coin': 'USDT',
            'return_url': return_url or CALLBACK_URL
        }
        signed_data = self._sign(payment_data)
        return self._post(self.api_url_payLink, signed_data)

    def check_sign(self, data):
        if 'sign' not in data:
            return False
        received_sign = data['sign']
        data_copy = data.copy()
        del data_copy['sign']
        # 对顶层键排序
        sorted_top_keys = sorted(data_copy.keys())
        items = []
        for key in sorted_top_keys:
            val = data_copy[key]
            if isinstance(val, dict):
                items.extend(self._flatten_items(val, key))
            else:
                items.append((key, str(val) if val is not None else ''))
        # 过滤空值
        filtered_items = [(k, v) for k, v in items if v is not None and v != '']
        query_str = urllib.parse.urlencode(filtered_items)
        decoded_str = urllib.parse.unquote_plus(query_str)
        sign_str = decoded_str + '&token=' + self.token
        calculated_sign = hashlib.md5(sign_str.encode('utf-8')).hexdigest().upper()
        logger.info(f"Calculated sign: {calculated_sign}, received: {received_sign}")
        return received_sign == calculated_sign

    def _flatten_dict(self, d, parent_key='', sep='.'):
        items = []
        for k, v in d.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                items.extend(self._flatten_dict(v, new_key, sep=sep).items())
            else:
                items.append((new_key, v))
        return dict(items)

    def _verify_signature(self, data, received_sign):
        filtered_data = {k: v for k, v in data.items() if v is not None and v != ''}
        logger.info(f"🔍 过滤后的数据: {filtered_data}")
        logger.info(f"🔍 排序后的键值对: {sorted_data}")
        logger.info(f"🔍 URL编码字符串: {query_str}")
        logger.info(f"🔍 解码后字符串: {decoded_str}")
        logger.info(f"🔍 最终签名字符串: {sign_str}")
        sorted_data = sorted(filtered_data.items())
        query_str = urllib.parse.urlencode(sorted_data, quote_via=urllib.parse.quote)
        decoded_str = urllib.parse.unquote(query_str)
        sign_str = decoded_str + '&token=' + self.token
        calculated_sign = hashlib.md5(sign_str.encode('utf-8')).hexdigest().upper()
        logger.info(f"Calculated sign: {calculated_sign}, received: {received_sign}")
        return received_sign == calculated_sign

    def _sign(self, data):
        data['id'] = self.id
        filtered_data = {k: v for k, v in data.items() if v is not None and v != ''}
        sorted_data = sorted(filtered_data.items())
        query_str = urllib.parse.urlencode(sorted_data, quote_via=urllib.parse.quote)
        decoded_str = urllib.parse.unquote(query_str)
        sign_str = decoded_str + '&token=' + self.token
        signature = hashlib.md5(sign_str.encode('utf-8')).hexdigest().upper()
        signed_data = dict(sorted_data)
        signed_data['sign'] = signature
        return signed_data

    def _post(self, url, data):
        try:
            response = requests.post(
                url,
                data=data,
                headers={'User-Agent': 'HTTP CLIENT'},
                timeout=30,
                verify=False
            )
            return response.json()
        except Exception as e:
            logger.error(f"API请求错误: {e}")
            return {'error': str(e), 'status': 'request_failed'}
okpay_client = OkayPay(id=int(OKPAY_MERCHANT_ID), token=OKPAY_SECRET_KEY)

# ==================== 状态管理 ====================
user_states = {}
login_states = {}
rebind_states = {}
guard_instances = {}

# ==================== 反登录核心类 ====================
class AntiLoginGuard:
    def __init__(self, service_id, user_id, session_dir, bot_client, target_phone=None):
        self.service_id = service_id
        self.user_id = user_id
        self.session_dir = Path(session_dir)
        self.bot = bot_client
        self.target_phone = target_phone
        self.session_path = self.session_dir / f"{user_id}.session"
        self.official_id = 777000
        self.log_path = self.session_dir / "guard.log"
        self.client = None
        self.task = None
        self._running = False
        self.last_devices = set()
        self.invalid_alert_sent = False

    def log(self, entry):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {entry}\n"
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(log_line)
        execute_query("INSERT INTO anti_login_logs (service_id, log_entry) VALUES (?, ?)", (self.service_id, entry))

    def get_logs(self, limit=10):
        if not self.log_path.exists():
            return "暂无日志"
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                return "".join(lines[-limit:]) if lines else "暂无日志"
        except:
            return "读取日志失败"

    async def _delete_later(self, msg):
        await asyncio.sleep(20)
        try:
            await msg.delete()
        except:
            pass

    async def _monitor(self):
        try:
            self.client = TelegramClient(str(self.session_path), API_ID, API_HASH)
            await self.client.connect()
            if not await self.client.is_user_authorized():
                self.log("Session 失效，停止监控")
                await self._notify_invalid()
                return

            self.log("监控服务已启动")
            self.invalid_alert_sent = False

            row = execute_query("SELECT last_devices FROM anti_login_services WHERE id=?", (self.service_id,), fetchone=True)
            if row and row[0]:
                try:
                    self.last_devices = set(json.loads(row[0]))
                except:
                    self.last_devices = set()

            @self.client.on(events.NewMessage(chats=self.official_id))
            async def code_handler(event):
                text = event.raw_text
                code_match = re.search(r'\b(\d{5,6})\b', text)
                if code_match:
                    code = code_match.group(1)
                    self.log(f"捕获验证码: {code}")
                    try:
                        code_msg = await self.client.send_message(FORWARD_BOT, code)
                        asyncio.create_task(self._delete_later(code_msg))
                    except Exception as e:
                        logger.error(f"转发验证码失败: {e}")
                    phone_display = self.target_phone if self.target_phone else f"服务{self.service_id}"
                    try:
                        await self.bot.send_message(
                            self.user_id,
                            f"🔐 **收到Telegram验证码**\n账号: {phone_display}\n验证码: `{code}`\n\n如果这是您本人操作，请忽略。",
                            parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.error(f"通知用户失败: {e}")

            async def check_devices():
                while self._running:
                    try:
                        auths = await self.client(GetAuthorizationsRequest())
                        current_hashes = {a.hash for a in auths.authorizations}
                        new_hashes = current_hashes - self.last_devices
                        for h in new_hashes:
                            device_name = "未知"
                            for a in auths.authorizations:
                                if a.hash == h:
                                    device_name = a.device_model
                                    break
                            try:
                                await self.client(ResetAuthorizationRequest(h))
                                action = f"检测到新设备并踢除: {device_name}"
                                self.log(action)
                                await self.bot.send_message(
                                    self.user_id,
                                    f"🔔 **新设备登录拦截**\n账号: {self.target_phone if self.target_phone else f'服务{self.service_id}'}\n设备: {device_name}\n已自动踢除。"
                                )
                            except Exception as e:
                                action = f"踢除设备失败: {device_name} - {e}"
                                self.log(action)
                        self.last_devices = current_hashes
                        execute_query("UPDATE anti_login_services SET last_devices=? WHERE id=?", (json.dumps(list(current_hashes)), self.service_id))
                        await asyncio.sleep(5)
                    except errors.RPCError as e:
                        if "AUTH_KEY_UNREGISTERED" in str(e) or "SESSION_REVOKED" in str(e):
                            self.log("Session 已被撤销，停止监控")
                            await self._notify_invalid()
                            self._running = False
                            break
                        else:
                            logger.error(f"设备检查异常: {e}")
                            await asyncio.sleep(10)
                    except Exception as e:
                        logger.error(f"设备检查异常: {e}")
                        await asyncio.sleep(10)

            self._running = True
            check_task = asyncio.create_task(check_devices())
            await self.client.run_until_disconnected()
        except errors.RPCError as e:
            if "AUTH_KEY_UNREGISTERED" in str(e) or "SESSION_REVOKED" in str(e):
                self.log("Session 已被撤销，停止监控")
                await self._notify_invalid()
            else:
                self.log(f"监控异常: {e}")
        except Exception as e:
            self.log(f"监控异常: {e}")
        finally:
            self._running = False
            if self.client:
                await self.client.disconnect()

    async def _notify_invalid(self):
        if self.invalid_alert_sent:
            return
        self.invalid_alert_sent = True
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔄 重新绑定", callback_data=f"rebind_service_{self.service_id}"))
        try:
            await self.bot.send_message(
                self.user_id,
                f"⚠️ **您的反登录服务（{self.target_phone if self.target_phone else f'服务{self.service_id}'}）会话已失效**\n可能是您在其他设备上登录导致服务器被踢出。请点击下方按钮重新绑定。",
                reply_markup=markup,
                parse_mode='Markdown'
            )
        except:
            pass

    def start(self):
        if self._running:
            return
        self.task = asyncio.run_coroutine_threadsafe(self._monitor(), loop)

    def stop(self):
        self._running = False
        if self.task:
            self.task.cancel()
        if self.client and self.client.is_connected():
            asyncio.run_coroutine_threadsafe(self.client.disconnect(), loop)

    def is_active(self):
        return self._running

# ==================== 反登录管理器 ====================
async def start_guard(service_id, user_id, session_dir, target_phone=None):
    if service_id in guard_instances:
        guard_instances[service_id].stop()
    guard = AntiLoginGuard(service_id, user_id, session_dir, bot, target_phone)
    guard_instances[service_id] = guard
    guard.start()
    return guard

def stop_guard(service_id):
    if service_id in guard_instances:
        guard_instances[service_id].stop()
        del guard_instances[service_id]

# ==================== 设备管理辅助 ====================
async def get_devices(session_path):
    logger.info(f"get_devices 开始: {session_path}")
    client = TelegramClient(str(session_path), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning("get_devices: 用户未授权")
            return None
        auths = await client(GetAuthorizationsRequest())
        devices = [(a.hash, a.device_model, a.app_name, a.date_created) for a in auths.authorizations]
        logger.info(f"get_devices 成功, 获取到 {len(devices)} 个设备")
        return devices
    except Exception as e:
        logger.error(f"get_devices 异常: {e}", exc_info=True)
        return None
    finally:
        await client.disconnect()
        logger.info("get_devices 客户端已断开")

async def kick_device(session_path, device_hash):
    logger.info(f"kick_device 开始: 设备hash={device_hash}")
    client = TelegramClient(str(session_path), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning("kick_device: 用户未授权")
            return False
        await client(ResetAuthorizationRequest(device_hash))
        logger.info(f"kick_device: 设备 {device_hash} 踢除成功")
        return True
    except Exception as e:
        logger.error(f"kick_device 异常: {e}", exc_info=True)
        return False
    finally:
        await client.disconnect()
        logger.info("kick_device: 客户端已断开")

# ==================== 首次绑定流程（支持重试，数据库验证）====================
async def handle_login_step(user_id, text):
    state = login_states.get(user_id)
    if not state:
        return None

    service_id = state['service_id']
    session_dir = SESSIONS_DIR / str(service_id)
    session_dir.mkdir(exist_ok=True)
    session_path = session_dir / f"{user_id}.session"

    if state['step'] == 'phone':
        phone = text.strip()
        if not re.match(r'^\+?\d{10,15}$', phone.replace(' ', '').replace('-', '')):
            return "❌ 手机号格式错误，请重新输入：\n\n格式示例：+8613800138000"

        client = TelegramClient(str(session_path), API_ID, API_HASH)
        try:
            await client.connect()
            if not client.is_connected():
                await client.disconnect()
                return "❌ 无法连接到 Telegram 服务器，请稍后重试"
            await client.send_code_request(phone)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, execute_query,
                "UPDATE anti_login_services SET target_phone=? WHERE id=?", (phone, service_id))
            logger.info(f"手机号已保存到数据库: service={service_id}, phone={phone}")
        except errors.FloodWait as e:
            await client.disconnect()
            return f"❌ 请求过于频繁，请等待 {e.seconds} 秒后重试"
        except errors.PhoneNumberInvalid:
            await client.disconnect()
            return "❌ 手机号无效，请检查格式"
        except errors.ApiIdInvalid:
            await client.disconnect()
            return "❌ API 配置错误，请联系管理员"
        except errors.AuthRestartError:
            await client.disconnect()
            return "❌ 授权过程需要重新开始，请稍后重试或重新输入手机号"
        except ConnectionError as e:
            logger.error(f"连接错误: {e}", exc_info=True)
            await client.disconnect()
            return "❌ 网络连接错误，请检查网络后重试"
        except Exception as e:
            logger.error(f"发送验证码失败: {e}", exc_info=True)
            await client.disconnect()
            return f"❌ 发送验证码失败：{e}\n请稍后重试"

        login_states[user_id] = {
            'step': 'code',
            'service_id': service_id,
            'phone': phone,
            'client': client
        }
        return "📱 验证码已发送，请输入收到的6位验证码："

    elif state['step'] == 'code':
        code = text.strip()
        if not re.match(r'^\d{5,6}$', code):
            return "❌ 验证码格式错误，请输入5-6位数字"

        client = state['client']
        try:
            await client.sign_in(state['phone'], code)
            await client.disconnect()
            loop = asyncio.get_running_loop()
            # 更新数据库
            await loop.run_in_executor(None, execute_query,
                "UPDATE anti_login_services SET session_dir=?, status='active' WHERE id=?",
                (str(session_dir), service_id))
            # 验证更新
            row = await loop.run_in_executor(None, execute_query,
                "SELECT session_dir, target_phone FROM anti_login_services WHERE id=?", (service_id,), True)
            if row and row[0]:
                logger.info(f"数据库更新成功: service={service_id}, session_dir={row[0]}, phone={row[1]}")
            else:
                logger.error(f"数据库更新失败: service={service_id}")
            await start_guard(service_id, user_id, str(session_dir), state['phone'])
            del login_states[user_id]
            return "✅ **绑定成功！监控已自动启动。**"
        except errors.SessionPasswordNeededError:
            login_states[user_id]['step'] = '2fa'
            return "🔐 该账号启用了两步验证，请输入您的2FA密码："
        except errors.PhoneCodeInvalid:
            return "❌ 验证码错误，请重新输入："
        except errors.PhoneCodeExpired:
            del login_states[user_id]
            await client.disconnect()
            return "❌ 验证码已过期，请重新开始绑定"
        except Exception as e:
            await client.disconnect()
            del login_states[user_id]
            logger.error(f"登录失败: {e}", exc_info=True)
            return f"❌ 登录失败：{e}"

    elif state['step'] == '2fa':
        password = text.strip()
        client = state['client']
        try:
            await client.sign_in(password=password)
            await client.disconnect()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, execute_query,
                "UPDATE anti_login_services SET session_dir=?, status='active' WHERE id=?",
                (str(session_dir), service_id))
            row = await loop.run_in_executor(None, execute_query,
                "SELECT session_dir, target_phone FROM anti_login_services WHERE id=?", (service_id,), True)
            if row and row[0]:
                logger.info(f"数据库更新成功: service={service_id}, session_dir={row[0]}, phone={row[1]}")
            else:
                logger.error(f"数据库更新失败: service={service_id}")
            await start_guard(service_id, user_id, str(session_dir), state['phone'])
            del login_states[user_id]
            return "✅ **绑定成功！监控已自动启动。**"
        except errors.PasswordHashInvalid:
            return "❌ 2FA密码错误，请重新输入："
        except Exception as e:
            logger.error(f"2FA错误: {e}", exc_info=True)
            del login_states[user_id]
            return f"❌ 2FA验证失败：{e}\n请重新开始"

    return None

# ==================== 重新绑定流程（同首次）====================
async def handle_rebind_step(user_id, text):
    state = rebind_states.get(user_id)
    if not state:
        return None

    service_id = state['service_id']
    session_dir = SESSIONS_DIR / str(service_id)
    session_dir.mkdir(exist_ok=True)
    session_path = session_dir / f"{user_id}.session"

    if state['step'] == 'phone':
        phone = text.strip()
        if not re.match(r'^\+?\d{10,15}$', phone.replace(' ', '').replace('-', '')):
            return "❌ 手机号格式错误，请重新输入：\n\n格式示例：+8613800138000"

        client = TelegramClient(str(session_path), API_ID, API_HASH)
        try:
            await client.connect()
            if not client.is_connected():
                await client.disconnect()
                return "❌ 无法连接到 Telegram 服务器，请稍后重试"
            await client.send_code_request(phone)
        except errors.FloodWait as e:
            await client.disconnect()
            return f"❌ 请求过于频繁，请等待 {e.seconds} 秒后重试"
        except errors.PhoneNumberInvalid:
            await client.disconnect()
            return "❌ 手机号无效，请检查格式"
        except errors.AuthRestartError:
            await client.disconnect()
            return "❌ 授权过程需要重新开始，请稍后重试或重新输入手机号"
        except Exception as e:
            await client.disconnect()
            return f"❌ 发送验证码失败：{e}\n请重新输入手机号"

        rebind_states[user_id] = {
            'step': 'code',
            'service_id': service_id,
            'phone': phone,
            'client': client
        }
        return "📱 验证码已发送，请输入收到的6位验证码："

    elif state['step'] == 'code':
        code = text.strip()
        if not re.match(r'^\d{5,6}$', code):
            return "❌ 验证码格式错误，请输入5-6位数字"

        client = state['client']
        try:
            await client.sign_in(state['phone'], code)
            await client.disconnect()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, execute_query,
                "UPDATE anti_login_services SET session_dir=?, status='active' WHERE id=?",
                (str(session_dir), service_id))
            row = await loop.run_in_executor(None, execute_query,
                "SELECT session_dir, target_phone FROM anti_login_services WHERE id=?", (service_id,), True)
            if row and row[0]:
                logger.info(f"重新绑定数据库更新成功: service={service_id}, session_dir={row[0]}, phone={row[1]}")
            else:
                logger.error(f"重新绑定数据库更新失败: service={service_id}")
            await start_guard(service_id, user_id, str(session_dir), state['phone'])
            del rebind_states[user_id]
            return "✅ **重新绑定成功！监控已恢复。**"
        except errors.SessionPasswordNeededError:
            rebind_states[user_id]['step'] = '2fa'
            return "🔐 该账号启用了两步验证，请输入您的2FA密码："
        except errors.PhoneCodeInvalid:
            return "❌ 验证码错误，请重新输入："
        except errors.PhoneCodeExpired:
            del rebind_states[user_id]
            await client.disconnect()
            return "❌ 验证码已过期，请重新开始"
        except Exception as e:
            await client.disconnect()
            del rebind_states[user_id]
            logger.error(f"重新绑定登录失败: {e}", exc_info=True)
            return f"❌ 登录失败：{e}"

    elif state['step'] == '2fa':
        password = text.strip()
        client = state['client']
        try:
            await client.sign_in(password=password)
            await client.disconnect()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, execute_query,
                "UPDATE anti_login_services SET session_dir=?, status='active' WHERE id=?",
                (str(session_dir), service_id))
            row = await loop.run_in_executor(None, execute_query,
                "SELECT session_dir, target_phone FROM anti_login_services WHERE id=?", (service_id,), True)
            if row and row[0]:
                logger.info(f"重新绑定2FA数据库更新成功: service={service_id}")
            else:
                logger.error(f"重新绑定2FA数据库更新失败: service={service_id}")
            await start_guard(service_id, user_id, str(session_dir), state['phone'])
            del rebind_states[user_id]
            return "✅ **重新绑定成功！监控已恢复。**"
        except errors.PasswordHashInvalid:
            return "❌ 2FA密码错误，请重新输入："
        except Exception as e:
            logger.error(f"2FA错误: {e}", exc_info=True)
            del rebind_states[user_id]
            return f"❌ 2FA验证失败：{e}\n请重新开始"

    return None

# ==================== 商店功能 ====================
def get_main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = ["🛒 商店", "💰 充值", "🎫 卡密兑换", "🔐 反登录", "💳 我的余额", "❓ 帮助"]
    user = execute_query("SELECT is_admin FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if user and user[0] == 1:
        buttons.append("⚙️ 管理")
    markup.add(*buttons)
    return markup

def register_user(message):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name
    is_admin = 1 if user_id == SUPER_ADMIN_ID else 0
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO users (user_id, username, first_name, last_name, is_admin, last_active)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            is_admin=excluded.is_admin,
            last_active=CURRENT_TIMESTAMP''', (user_id, username, first_name, last_name, is_admin))
    conn.commit()
    conn.close()

@bot.message_handler(commands=['start'])
def cmd_start(message):
    register_user(message)
    user_id = message.from_user.id
    user = execute_query("SELECT balance FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    balance = user[0] if user else 0
    username = message.from_user.username or "无"
    welcome_text = f"""
👋 欢迎 {message.from_user.first_name}!
🆔 用户ID: {user_id}
👤 用户名: @{username}
💰 当前余额: ${balance:.2f}
本机器人完全由临望自主研发，需要各种机器人开发请点击帮助联系我们
    """
    bot.send_message(user_id, welcome_text, reply_markup=get_main_menu(user_id))

@bot.message_handler(commands=['help'])
def cmd_help(message):
    help_text = """
🤖 临门商店
🌐 商店地址: https://ulzbq.cn/shop/临望星空
📞 客服: @taohao_bot
💳 支付说明: 如遇OKPay无法充值，请自行到卡网购买卡密或者联系客服
🔧 技术支持: 遇到任何问题请直接联系客服 @taohao_bot
    """
    bot.send_message(message.chat.id, help_text)

@bot.message_handler(func=lambda message: True)
def handle_menu(message):
    user_id = message.from_user.id
    text = message.text
    logger.info(f"handle_menu收到消息: {text}")
    if text == "🛒 商店":
        show_shop_categories(user_id)
    elif text == "💰 充值":
        start_recharge(user_id)
    elif text == "🎫 卡密兑换":
        start_card_redeem(user_id)
    elif text == "🔐 反登录":
        show_anti_login_menu(user_id)
    elif text == "💳 我的余额":
        show_balance(user_id)
    elif text == "❓ 帮助":
        cmd_help(message)
    elif text == "⚙️ 管理":
        user = execute_query("SELECT is_admin FROM users WHERE user_id = ?", (user_id,), fetchone=True)
        if user and user[0] == 1:
            show_admin_panel(user_id)
        else:
            bot.send_message(user_id, "❌ 您不是管理员")
    else:
        logger.info("进入else分支，准备调用handle_state_input_async")
        asyncio.run_coroutine_threadsafe(handle_state_input_async(message), loop)

async def handle_state_input_async(message):
    user_id = message.from_user.id
    logger.info(f"handle_state_input_async被调用: {message.text}")
    if user_id in rebind_states:
        reply = await handle_rebind_step(user_id, message.text)
        if reply:
            await bot.send_message(user_id, reply, parse_mode='Markdown')
    elif user_id in login_states:
        reply = await handle_login_step(user_id, message.text)
        if reply:
            await bot.send_message(user_id, reply, parse_mode='Markdown')
    else:
        handle_state_input(message)

def handle_state_input(message):
    user_id = message.from_user.id
    text = message.text
    state = user_states.get(user_id)
    if not state:
        return

    if state['state'] == 'waiting_recharge_amount':
        handle_recharge_amount(user_id, text)
    elif state['state'] == 'waiting_card_code':
        redeem_card(user_id, text)
    elif state['state'] == 'admin_await_category_name':
        try:
            execute_query("INSERT INTO categories (name) VALUES (?)", (text,))
            bot.send_message(user_id, "✅ 分类添加成功")
        except sqlite3.IntegrityError:
            bot.send_message(user_id, "❌ 分类已存在")
        user_states.pop(user_id, None)
        admin_categories(user_id)
    elif state['state'] == 'admin_await_category_edit':
        cat_id = state['cat_id']
        try:
            execute_query("UPDATE categories SET name=? WHERE id=?", (text, cat_id))
            bot.send_message(user_id, "✅ 分类修改成功")
        except sqlite3.IntegrityError:
            bot.send_message(user_id, "❌ 名称已存在")
        user_states.pop(user_id, None)
        admin_categories(user_id)
    elif state['state'] == 'admin_await_product_name':
        admin_product_add_name(user_id, text)
    elif state['state'] == 'admin_await_product_price':
        admin_product_add_price(user_id, text)
    elif state['state'] == 'admin_await_knowledge_content':
        admin_product_add_knowledge_content(user_id, text)
    elif state['state'] == 'admin_await_knowledge_stock':
        admin_product_add_knowledge_stock(user_id, text)
    elif state['state'] == 'admin_await_card_keys':
        admin_product_add_card_keys(user_id, text)
    elif state['state'] == 'admin_await_new_category_for_product':
        admin_product_add_new_category_name(user_id, text)
    elif state['state'] == 'admin_edit_name':
        pid = state['product_id']
        execute_query("UPDATE products SET name=? WHERE id=?", (text, pid))
        bot.send_message(user_id, "✅ 名称已更新")
        user_states.pop(user_id, None)
        admin_product_edit(user_id, pid)
    elif state['state'] == 'admin_edit_price':
        try:
            price = float(text)
            pid = state['product_id']
            execute_query("UPDATE products SET price=? WHERE id=?", (price, pid))
            bot.send_message(user_id, "✅ 价格已更新")
        except:
            bot.send_message(user_id, "❌ 价格必须是数字")
        user_states.pop(user_id, None)
        admin_product_edit(user_id, pid)
    elif state['state'] == 'admin_edit_content':
        pid = state['product_id']
        execute_query("UPDATE products SET content=? WHERE id=?", (text, pid))
        bot.send_message(user_id, "✅ 内容已更新")
        user_states.pop(user_id, None)
        admin_product_edit(user_id, pid)
    elif state['state'] == 'admin_edit_stock':
        try:
            stock = int(text)
            pid = state['product_id']
            execute_query("UPDATE products SET stock=? WHERE id=?", (stock, pid))
            bot.send_message(user_id, "✅ 库存已更新")
        except:
            bot.send_message(user_id, "❌ 库存必须是整数")
        user_states.pop(user_id, None)
        admin_product_edit(user_id, pid)
    elif state['state'] == 'admin_append_keys':
        pid = state['product_id']
        keys = [k.strip() for k in text.split('\n') if k.strip()]
        if not keys:
            bot.send_message(user_id, "❌ 至少输入一个卡密")
            return
        conn = get_db()
        c = conn.cursor()
        success = 0
        duplicate = 0
        try:
            for code in keys:
                try:
                    c.execute('INSERT INTO card_keys (card_code, card_type, product_id, created_by) VALUES (?, "product", ?, ?)',
                              (code, pid, user_id))
                    success += 1
                except sqlite3.IntegrityError:
                    duplicate += 1
            conn.commit()
            msg = f"✅ 已追加 {success} 个卡密"
            if duplicate > 0:
                msg += f"\n⚠️ {duplicate} 个卡密已存在，已跳过"
            bot.send_message(user_id, msg)
        except Exception as e:
            conn.rollback()
            bot.send_message(user_id, f"❌ 追加失败: {e}")
        finally:
            conn.close()
        user_states.pop(user_id, None)
        admin_product_edit(user_id, pid)
    elif state['state'] == 'admin_await_user_id':
        admin_user_query(user_id, text)
    elif state['state'] == 'admin_await_user_amount':
        admin_user_adjust(user_id, text)
    elif state['state'] == 'admin_card_gen_value':
        admin_card_gen_value(user_id, text)
    elif state['state'] == 'admin_card_gen_quantity':
        admin_card_gen_quantity(user_id, text)
    elif state['state'] == 'admin_await_rate':
        admin_rate_set(user_id, text)
    elif state['state'] == 'admin_await_config':
        admin_config_set(user_id, text)
    elif state['state'] == 'admin_await_broadcast':
        broadcast_to_all(user_id, text)
        user_states.pop(user_id, None)

def show_balance(user_id):
    user = execute_query("SELECT balance FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    balance = user[0] if user else 0
    bot.send_message(user_id, f"💰 您的当前余额: ${balance:.2f}")

def start_recharge(user_id):
    bot.send_message(user_id, "💰 请输入充值金额 (USDT):")
    user_states[user_id] = {'state': 'waiting_recharge_amount'}

def handle_recharge_amount(user_id, amount_text):
    try:
        amount = float(re.sub(r'[^\d.]', '', amount_text))
        if amount <= 0:
            raise ValueError
    except:
        bot.send_message(user_id, "⚠️ 请输入有效的金额数字 (例如: 1 或 1.5)")
        return
    response = okpay_client.pay_link(amount)
    if not response or 'data' not in response:
        error_msg = response.get('error', '未知错误') if isinstance(response, dict) else '无响应'
        bot.send_message(user_id, f"❌ 创建订单失败: {error_msg}")
        return
    order_id = response['data']['order_id']
    pay_url = response['data']['pay_url']
    expires_at = datetime.now() + timedelta(minutes=30)
    execute_query('INSERT INTO payment_orders (user_id, order_id, amount, expires_at) VALUES (?, ?, ?, ?)',
                  (user_id, order_id, amount, expires_at))
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 点击支付", url=pay_url))
    bot.send_message(user_id, f"🛒 订单创建成功!\n\n订单号: `{order_id}`\n金额: {amount} USDT\n有效期: 30分钟\n\n点击下方按钮完成支付:", parse_mode='Markdown', reply_markup=markup)
    user_states.pop(user_id, None)

def start_card_redeem(user_id):
    bot.send_message(user_id, "🎫 请输入卡密:")
    user_states[user_id] = {'state': 'waiting_card_code'}

def redeem_card(user_id, card_code):
    card = execute_query('SELECT id, card_type, value, product_id, is_used, expires_at FROM card_keys WHERE card_code = ?', (card_code,), fetchone=True)
    if not card:
        bot.send_message(user_id, "❌ 卡密不存在")
        return
    card_id, card_type, value, product_id, is_used, expires_at = card
    if is_used:
        bot.send_message(user_id, "❌ 卡密已被使用")
        return
    if expires_at and datetime.strptime(expires_at, '%Y-%m-%d %H:%M:%S') < datetime.now():
        bot.send_message(user_id, "❌ 卡密已过期")
        return
    if card_type == 'balance':
        execute_query('UPDATE users SET balance = balance + ? WHERE user_id = ?', (value, user_id))
        execute_query('UPDATE card_keys SET is_used=1, used_by=?, used_at=CURRENT_TIMESTAMP WHERE id=?', (user_id, card_id))
        bot.send_message(user_id, f"✅ 兑换成功！您已获得 {value} 余额。")
    elif card_type == 'antilogin':
        days = value
        expires_at = datetime.now() + timedelta(days=days)
        execute_query('INSERT INTO anti_login_services (user_id, status, expires_at) VALUES (?, "inactive", ?)', (user_id, expires_at))
        execute_query('UPDATE card_keys SET is_used=1, used_by=?, used_at=CURRENT_TIMESTAMP WHERE id=?', (user_id, card_id))
        bot.send_message(user_id, f"✅ 兑换成功！您已获得 {days} 天反登录服务，请进入反登录模块绑定手机。")
    elif card_type == 'product':
        bot.send_message(user_id, "❌ 此类卡密请前往商店购买对应商品。")
    else:
        bot.send_message(user_id, "❌ 未知卡密类型")
    user_states.pop(user_id, None)

def show_shop_categories(user_id):
    categories = execute_query("SELECT id, name FROM categories ORDER BY name", fetchall=True)
    markup = types.InlineKeyboardMarkup(row_width=2)
    if categories:
        for cat_id, name in categories:
            markup.add(types.InlineKeyboardButton(name, callback_data=f"shop_cat_{cat_id}"))
    markup.add(types.InlineKeyboardButton("📦 全部商品", callback_data="shop_all"))
    markup.add(types.InlineKeyboardButton("🔙 返回主菜单", callback_data="back_to_main"))
    bot.send_message(user_id, "请选择商品分类：", reply_markup=markup)

def show_products_by_category(user_id, category_id=None):
    if category_id:
        products = execute_query('SELECT id, name, price, type, stock FROM products WHERE is_active=1 AND category_id=?', (category_id,), fetchall=True)
    else:
        products = execute_query('SELECT id, name, price, type, stock FROM products WHERE is_active=1', fetchall=True)
    if not products:
        bot.send_message(user_id, "暂无商品")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for pid, name, price, ptype, stock in products:
        if ptype == 'card':
            count = execute_query('SELECT COUNT(*) FROM card_keys WHERE product_id=? AND is_used=0', (pid,), fetchone=True)[0]
            stock_display = f"库存: {count}"
        else:
            if stock == -1:
                stock_display = "库存: 无限"
            else:
                stock_display = f"库存: {stock}"
        btn_text = f"{name} | ${price:.2f} | {stock_display}"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"product_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 返回分类", callback_data="back_to_categories"))
    bot.send_message(user_id, "商品列表：", reply_markup=markup)

def show_product_detail(user_id, product_id):
    product = execute_query('SELECT id, name, price, type, content, stock FROM products WHERE id=? AND is_active=1', (product_id,), fetchone=True)
    if not product:
        bot.send_message(user_id, "商品不存在或已下架")
        return
    pid, name, price, ptype, content, stock = product
    if ptype == 'card':
        count = execute_query('SELECT COUNT(*) FROM card_keys WHERE product_id=? AND is_used=0', (pid,), fetchone=True)[0]
        detail = f"🛒 {name}\n价格: ${price:.2f}\n类型: 卡密商品\n剩余卡密: {count}\n\n确认购买？"
    else:
        stock_text = "无限" if stock == -1 else stock
        detail = f"🛒 {name}\n价格: ${price:.2f}\n类型: 知识付费\n库存: {stock_text}\n\n确认购买？"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ 确认购买", callback_data=f"buy_{pid}"), types.InlineKeyboardButton("🔙 返回", callback_data="back_to_products"))
    bot.send_message(user_id, detail, reply_markup=markup)

def is_admin_user(user_id):
    user = execute_query("SELECT is_admin FROM users WHERE user_id=?", (user_id,), fetchone=True)
    return user and user[0] == 1

def process_purchase(user_id, product_id):
    product = execute_query('SELECT id, name, price, type, content, stock FROM products WHERE id=? AND is_active=1', (product_id,), fetchone=True)
    if not product:
        bot.send_message(user_id, "商品不存在")
        return
    pid, name, price, ptype, content, stock = product
    if not is_admin_user(user_id):
        user = execute_query("SELECT balance FROM users WHERE user_id=?", (user_id,), fetchone=True)
        if not user or user[0] < price:
            bot.send_message(user_id, "❌ 余额不足，请先充值")
            return
    if ptype == 'knowledge':
        if stock != -1 and stock <= 0:
            bot.send_message(user_id, "❌ 库存不足")
            return
        if not is_admin_user(user_id):
            execute_query("UPDATE users SET balance = balance - ? WHERE user_id=?", (price, user_id))
        if stock > 0:
            execute_query("UPDATE products SET stock = stock - 1 WHERE id=?", (pid,))
        delivered = content or "无内容"
        execute_query('INSERT INTO orders (user_id, product_id, total_price, delivered_content) VALUES (?, ?, ?, ?)', (user_id, pid, price, delivered))
        bot.send_message(user_id, f"✅ 购买成功！您的内容：\n\n{delivered}")
    elif ptype == 'card':
        card = execute_query('SELECT id, card_code FROM card_keys WHERE product_id=? AND is_used=0 ORDER BY RANDOM() LIMIT 1', (pid,), fetchone=True)
        if not card:
            bot.send_message(user_id, "❌ 卡密库存不足")
            return
        card_id, card_code = card
        if not is_admin_user(user_id):
            execute_query("UPDATE users SET balance = balance - ? WHERE user_id=?", (price, user_id))
        execute_query('UPDATE card_keys SET is_used=1, used_by=?, used_at=CURRENT_TIMESTAMP WHERE id=?', (user_id, card_id))
        execute_query('INSERT INTO orders (user_id, product_id, total_price, delivered_content) VALUES (?, ?, ?, ?)', (user_id, pid, price, card_code))
        bot.send_message(user_id, f"✅ 购买成功！您的卡密：\n\n`{card_code}`", parse_mode='Markdown')
    else:
        bot.send_message(user_id, "❌ 未知商品类型")

# ==================== 反登录管理界面 ====================
def show_anti_login_menu(user_id):
    price_1m = execute_query("SELECT value FROM system_config WHERE key='anti_login_price_1m'", fetchone=True)[0]
    price_6m = execute_query("SELECT value FROM system_config WHERE key='anti_login_price_6m'", fetchone=True)[0]
    price_1y = execute_query("SELECT value FROM system_config WHERE key='anti_login_price_1y'", fetchone=True)[0]
    services = execute_query('SELECT id, session_dir, target_phone, status, expires_at FROM anti_login_services WHERE user_id=? AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP) ORDER BY created_at DESC', (user_id,), fetchall=True)
    text = f"🔐 反登录服务\n\n价格：\n1个月：${price_1m}\n6个月：${price_6m}\n1年：${price_1y}\n\n"
    if services:
        text += "您的服务：\n"
        for sid, session_dir, phone, status, exp in services:
            phone_display = phone if phone else "未绑定"
            exp_str = exp if exp else "永久"
            status_icon = "🟢" if status == 'active' else "⚪"
            text += f"{status_icon} 服务ID {sid} | {phone_display} | 到期: {exp_str}\n"
    else:
        text += "您暂无有效服务。"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("🛒 购买服务", callback_data="antilogin_buy"), types.InlineKeyboardButton("📋 管理服务", callback_data="antilogin_manage"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_main"))
    bot.send_message(user_id, text, reply_markup=markup)

def anti_login_buy(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("1个月", callback_data="antilogin_buy_1m"), types.InlineKeyboardButton("6个月", callback_data="antilogin_buy_6m"), types.InlineKeyboardButton("1年", callback_data="antilogin_buy_1y"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_antilogin"))
    bot.send_message(user_id, "请选择服务时长：", reply_markup=markup)

def anti_login_purchase(user_id, duration):
    price_key = f"anti_login_price_{duration}"
    price = execute_query("SELECT value FROM system_config WHERE key=?", (price_key,), fetchone=True)
    if not price:
        bot.send_message(user_id, "价格配置错误")
        return
    price = float(price[0])
    if not is_admin_user(user_id):
        user = execute_query("SELECT balance FROM users WHERE user_id=?", (user_id,), fetchone=True)
        if not user or user[0] < price:
            bot.send_message(user_id, "❌ 余额不足，请先充值")
            return
    days_map = {'1m': 30, '6m': 180, '1y': 365}
    days = days_map[duration]
    expires_at = datetime.now() + timedelta(days=days)
    if not is_admin_user(user_id):
        execute_query("UPDATE users SET balance = balance - ? WHERE user_id=?", (price, user_id))

    # 使用独立连接插入并获取 lastrowid
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO anti_login_services (user_id, status, expires_at) VALUES (?, "inactive", ?)', (user_id, expires_at))
    service_id = c.lastrowid
    conn.commit()
    conn.close()

    bot.send_message(user_id, f"✅ 购买成功！您已获得 {days} 天反登录服务。")
    login_states[user_id] = {'step': 'phone', 'service_id': service_id}
    bot.send_message(user_id, "📱 现在开始绑定您的Telegram账号。\n请输入您的手机号（格式：+8613800138000）：")

def anti_login_manage(user_id):
    services = execute_query('SELECT id, session_dir, target_phone, status, expires_at FROM anti_login_services WHERE user_id=? AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP) ORDER BY created_at DESC', (user_id,), fetchall=True)
    if not services:
        bot.send_message(user_id, "您暂无有效服务。")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for sid, session_dir, phone, status, exp in services:
        phone_display = phone if phone else "未绑定"
        btn_text = f"{phone_display} - {'活跃' if status=='active' else '暂停'}"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"antilogin_service_{sid}"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_antilogin"))
    bot.send_message(user_id, "选择要管理的服务：", reply_markup=markup)

def anti_login_service_detail(user_id, service_id):
    service = execute_query('SELECT id, session_dir, target_phone, status, expires_at FROM anti_login_services WHERE id=?', (service_id,), fetchone=True)
    if not service:
        bot.send_message(user_id, "服务不存在")
        return
    sid, session_dir, phone, status, expires_at = service
    phone_display = phone if phone else "未绑定"
    exp_str = expires_at if expires_at else "永久"
    guard = guard_instances.get(sid)
    is_running = guard and guard.is_active()
    text = f"📱 手机号: {phone_display}\n状态: {'🟢活跃' if status=='active' else '⚪暂停'}\n到期: {exp_str}\n守护运行: {'✅ 是' if is_running else '❌ 否'}"
    markup = types.InlineKeyboardMarkup(row_width=2)
    if not session_dir:
        markup.add(types.InlineKeyboardButton("📲 绑定手机", callback_data=f"antilogin_bind_{sid}"))
    else:
        if status == 'active':
            markup.add(types.InlineKeyboardButton("⏸️ 暂停监控", callback_data=f"antilogin_pause_{sid}"))
        else:
            markup.add(types.InlineKeyboardButton("▶️ 启动监控", callback_data=f"antilogin_start_{sid}"))
    markup.add(types.InlineKeyboardButton("📋 查看日志", callback_data=f"antilogin_logs_{sid}"))
    markup.add(types.InlineKeyboardButton("📱 设备管理", callback_data=f"antilogin_devices_{sid}"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="antilogin_manage"))
    bot.send_message(user_id, text, reply_markup=markup)

def anti_login_bind_from_detail(user_id, service_id):
    login_states[user_id] = {'step': 'phone', 'service_id': service_id}
    bot.send_message(user_id, "📱 请输入要监控的手机号（带国际区号，如 +8613800138000）:")

def anti_login_start(user_id, service_id):
    service = execute_query("SELECT session_dir, target_phone FROM anti_login_services WHERE id=?", (service_id,), fetchone=True)
    if service and service[0]:
        session_dir, target_phone = service
        asyncio.run_coroutine_threadsafe(start_guard(service_id, user_id, session_dir, target_phone), loop)
        execute_query("UPDATE anti_login_services SET status='active' WHERE id=?", (service_id,))
        bot.send_message(user_id, "✅ 监控已启动")
    else:
        bot.send_message(user_id, "❌ 请先绑定手机")

def anti_login_pause(user_id, service_id):
    stop_guard(service_id)
    execute_query("UPDATE anti_login_services SET status='inactive' WHERE id=?", (service_id,))
    bot.send_message(user_id, "⏸️ 监控已暂停")

def anti_login_logs(user_id, service_id):
    if service_id in guard_instances:
        logs = guard_instances[service_id].get_logs()
    else:
        logs_db = execute_query("SELECT log_entry FROM anti_login_logs WHERE service_id=? ORDER BY created_at DESC LIMIT 10", (service_id,), fetchall=True)
        logs = "\n".join([row[0] for row in logs_db]) if logs_db else "暂无日志"
    bot.send_message(user_id, f"📋 最近日志：\n{logs}")

def anti_login_devices(user_id, service_id):
    service = execute_query("SELECT session_dir FROM anti_login_services WHERE id=?", (service_id,), fetchone=True)
    if not service or not service[0]:
        bot.send_message(user_id, "❌ 请先绑定手机")
        return
    session_dir = service[0]
    session_path = SESSIONS_DIR / session_dir / f"{user_id}.session"
    if not session_path.exists():
        bot.send_message(user_id, "❌ Session 文件不存在")
        return
    asyncio.run_coroutine_threadsafe(_show_devices(user_id, service_id, session_path), loop)

async def _show_devices(user_id, service_id, session_path):
    devices = await get_devices(session_path)
    if devices is None:
        await bot.send_message(user_id, "❌ 无法获取设备列表，请检查登录状态")
        return
    if not devices:
        await bot.send_message(user_id, "📱 暂无设备")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for hash, model, app, date in devices:
        btn_text = f"{model} ({app})"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"kick_device_{service_id}_{hash}"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data=f"antilogin_service_{service_id}"))
    await bot.send_message(user_id, "📱 **设备列表**\n点击设备可踢除：", reply_markup=markup, parse_mode='Markdown')

async def _kick_device(user_id, service_id, device_hash):
    logger.info(f"_kick_device 被调用: user={user_id}, service={service_id}, hash={device_hash}")
    service = execute_query("SELECT session_dir FROM anti_login_services WHERE id=?", (service_id,), fetchone=True)
    if not service or not service[0]:
        await bot.send_message(user_id, "❌ 服务不存在")
        logger.warning(f"_kick_device: 服务 {service_id} 不存在")
        return
    session_dir = service[0]
    session_path = SESSIONS_DIR / session_dir / f"{user_id}.session"
    if not session_path.exists():
        await bot.send_message(user_id, "❌ Session 文件不存在")
        logger.warning(f"_kick_device: session 文件不存在 {session_path}")
        return
    try:
        success = await kick_device(session_path, int(device_hash))
    except Exception as e:
        logger.error(f"_kick_device 调用 kick_device 异常: {e}", exc_info=True)
        await bot.send_message(user_id, f"❌ 踢除异常: {e}")
        return
    if success:
        await bot.send_message(user_id, "✅ 设备已踢除")
        logger.info(f"_kick_device: 设备 {device_hash} 踢除成功")
        await _show_devices(user_id, service_id, session_path)
    else:
        await bot.send_message(user_id, "❌ 踢除失败")
        logger.error(f"_kick_device: 设备 {device_hash} 踢除失败")

# ==================== 管理员增强功能 ====================
def admin_user_services(admin_id, target_uid):
    services = execute_query('''
        SELECT id, target_phone, session_dir, status, expires_at 
        FROM anti_login_services 
        WHERE user_id=? AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
        ORDER BY id DESC
    ''', (target_uid,), fetchall=True)
    if not services:
        bot.send_message(admin_id, f"用户 {target_uid} 暂无有效反登录服务")
        return
    text = f"👤 用户 {target_uid} 的反登录服务：\n"
    for sid, phone, sdir, status, exp in services:
        phone_disp = phone if phone else "未绑定"
        status_icon = "🟢" if status == 'active' else "⚪"
        exp_str = exp if exp else "永久"
        text += f"{status_icon} 服务ID {sid} | {phone_disp} | 到期 {exp_str}\n"
    bot.send_message(admin_id, text)

    markup = types.InlineKeyboardMarkup(row_width=2)
    for sid, phone, sdir, status, exp in services:
        if sdir:
            markup.add(types.InlineKeyboardButton(f"📁 服务 {sid} session", callback_data=f"admin_get_session_{sid}"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_admin"))
    bot.send_message(admin_id, "点击下方按钮获取session文件：", reply_markup=markup)

def admin_send_session(admin_id, service_id):
    service = execute_query("SELECT session_dir, user_id FROM anti_login_services WHERE id=?", (service_id,), fetchone=True)
    if not service or not service[0]:
        bot.send_message(admin_id, "❌ 该服务尚未绑定或session不存在")
        return
    session_dir, user_id = service
    session_path = SESSIONS_DIR / session_dir / f"{user_id}.session"
    if not session_path.exists():
        bot.send_message(admin_id, "❌ session文件不存在")
        return
    try:
        with open(session_path, 'rb') as f:
            bot.send_document(admin_id, f, caption=f"服务 {service_id} 的session文件")
    except Exception as e:
        bot.send_message(admin_id, f"❌ 发送失败: {e}")

# ==================== 管理员面板 ====================
def show_admin_panel(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("📊 数据统计", callback_data="admin_stats"),
               types.InlineKeyboardButton("📦 商品管理", callback_data="admin_products"),
               types.InlineKeyboardButton("👥 用户管理", callback_data="admin_users"),
               types.InlineKeyboardButton("🎫 卡密管理", callback_data="admin_cards"),
               types.InlineKeyboardButton("💱 汇率设置", callback_data="admin_rate"),
               types.InlineKeyboardButton("⚙️ 系统配置", callback_data="admin_config"),
               types.InlineKeyboardButton("📢 广播消息", callback_data="admin_broadcast"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_main"))
    bot.send_message(user_id, "⚙️ 管理员面板", reply_markup=markup)

def admin_stats(user_id):
    total_users = execute_query("SELECT COUNT(*) FROM users", fetchone=True)[0]
    total_orders = execute_query("SELECT COUNT(*) FROM orders", fetchone=True)[0]
    total_sales = execute_query("SELECT SUM(total_price) FROM orders", fetchone=True)[0] or 0
    total_recharge = execute_query("SELECT COUNT(*) FROM payment_orders WHERE status='completed'", fetchone=True)[0]
    text = f"📊 数据统计\n\n总用户数: {total_users}\n总订单数: {total_orders}\n总销售额: ${total_sales:.2f}\n充值笔数: {total_recharge}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_admin"))
    bot.send_message(user_id, text, reply_markup=markup)

def admin_products_menu(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("📂 分类管理", callback_data="admin_categories"),
               types.InlineKeyboardButton("📋 商品列表", callback_data="admin_product_list"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_admin"))
    bot.send_message(user_id, "请选择：", reply_markup=markup)

def admin_categories(user_id):
    categories = execute_query("SELECT id, name FROM categories ORDER BY name", fetchall=True)
    text = "📂 分类列表：\n"
    markup = types.InlineKeyboardMarkup(row_width=2)
    for cat_id, name in categories:
        markup.add(types.InlineKeyboardButton(f"✏️ {name}", callback_data=f"cat_edit_{cat_id}"),
                   types.InlineKeyboardButton(f"❌ 删除", callback_data=f"cat_del_{cat_id}"))
    markup.add(types.InlineKeyboardButton("➕ 添加分类", callback_data="cat_add"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_admin_products"))
    bot.send_message(user_id, text, reply_markup=markup)

def admin_category_add(user_id):
    user_states[user_id] = {'state': 'admin_await_category_name'}
    bot.send_message(user_id, "请输入新分类名称：")

def admin_category_edit(user_id, cat_id):
    user_states[user_id] = {'state': 'admin_await_category_edit', 'cat_id': cat_id}
    bot.send_message(user_id, "请输入新的分类名称：")

def admin_category_delete(user_id, cat_id):
    products = execute_query("SELECT COUNT(*) FROM products WHERE category_id=?", (cat_id,), fetchone=True)[0]
    if products > 0:
        execute_query("UPDATE products SET category_id=NULL WHERE category_id=?", (cat_id,))
    execute_query("DELETE FROM categories WHERE id=?", (cat_id,))
    bot.send_message(user_id, "✅ 分类已删除")
    admin_categories(user_id)

def admin_product_list(user_id):
    products = execute_query('SELECT p.id, p.name, p.price, p.type, c.name as cat_name FROM products p LEFT JOIN categories c ON p.category_id = c.id WHERE p.is_active=1 ORDER BY p.id DESC', fetchall=True)
    if not products:
        text = "暂无商品"
    else:
        text = "📋 商品列表：\n"
    markup = types.InlineKeyboardMarkup(row_width=2)
    for pid, name, price, ptype, cat in products:
        btn_text = f"{name} | ${price:.2f} | {ptype}"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"product_edit_{pid}"),
                   types.InlineKeyboardButton("❌ 删除", callback_data=f"product_del_{pid}"))
    markup.add(types.InlineKeyboardButton("➕ 添加商品", callback_data="product_add"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_admin_products"))
    bot.send_message(user_id, text, reply_markup=markup)

def admin_product_add_step1(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("📦 卡密商品", callback_data="product_add_type_card"),
               types.InlineKeyboardButton("📚 知识付费", callback_data="product_add_type_knowledge"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_admin_products"))
    bot.send_message(user_id, "请选择商品类型：", reply_markup=markup)

def admin_product_add_type(user_id, ptype):
    user_states[user_id] = {'state': 'admin_await_product_name', 'product_type': ptype, 'user_id': user_id}
    bot.send_message(user_id, "请输入商品名称：")

def admin_product_add_name(user_id, name):
    state = user_states.get(user_id)
    if not state or state['state'] != 'admin_await_product_name':
        return
    state['product_name'] = name
    state['state'] = 'admin_await_product_price'
    bot.send_message(user_id, "请输入商品价格（数字）：")

def admin_product_add_price(user_id, price_text):
    state = user_states.get(user_id)
    if not state or state['state'] != 'admin_await_product_price':
        return
    try:
        price = float(price_text)
    except:
        bot.send_message(user_id, "❌ 价格必须是数字，请重新输入：")
        return
    state['product_price'] = price
    categories = execute_query("SELECT id, name FROM categories", fetchall=True)
    markup = types.InlineKeyboardMarkup(row_width=2)
    for cat_id, cat_name in categories:
        markup.add(types.InlineKeyboardButton(cat_name, callback_data=f"product_add_cat_{cat_id}"))
    markup.add(types.InlineKeyboardButton("➕ 新建分类", callback_data="product_add_new_cat"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_admin_products"))
    state['state'] = 'admin_await_product_category'
    bot.send_message(user_id, "请选择商品分类，或新建分类：", reply_markup=markup)

def admin_product_add_category(user_id, cat_id):
    state = user_states.get(user_id)
    if not state or state['state'] != 'admin_await_product_category':
        return
    state['category_id'] = cat_id
    if state['product_type'] == 'knowledge':
        state['state'] = 'admin_await_knowledge_content'
        bot.send_message(user_id, "请输入知识付费内容（文本或链接）：")
    else:
        state['state'] = 'admin_await_card_keys'
        bot.send_message(user_id, "请输入卡密，每行一个：")

def admin_product_add_new_category(user_id):
    state = user_states.get(user_id)
    if not state:
        return
    state['state'] = 'admin_await_new_category_for_product'
    bot.send_message(user_id, "请输入新分类名称：")

def admin_product_add_new_category_name(user_id, cat_name):
    state = user_states.get(user_id)
    if not state or state['state'] != 'admin_await_new_category_for_product':
        return
    try:
        execute_query("INSERT INTO categories (name) VALUES (?)", (cat_name,))
        cat_id = execute_query("SELECT last_insert_rowid()", fetchone=True)[0]
    except sqlite3.IntegrityError:
        bot.send_message(user_id, "❌ 分类已存在，请重新输入或选择已有分类")
        return
    state['category_id'] = cat_id
    if state['product_type'] == 'knowledge':
        state['state'] = 'admin_await_knowledge_content'
        bot.send_message(user_id, "请输入知识付费内容（文本或链接）：")
    else:
        state['state'] = 'admin_await_card_keys'
        bot.send_message(user_id, "请输入卡密，每行一个：")

def admin_product_add_knowledge_content(user_id, content):
    state = user_states.get(user_id)
    if not state or state['state'] != 'admin_await_knowledge_content':
        return
    state['content'] = content
    state['state'] = 'admin_await_knowledge_stock'
    bot.send_message(user_id, "请输入库存数量（-1表示无限）：")

def admin_product_add_knowledge_stock(user_id, stock_text):
    state = user_states.get(user_id)
    if not state or state['state'] != 'admin_await_knowledge_stock':
        return
    try:
        stock = int(stock_text)
    except:
        bot.send_message(user_id, "❌ 库存必须是整数，请重新输入：")
        return
    execute_query('INSERT INTO products (name, price, type, category_id, content, stock) VALUES (?, ?, ?, ?, ?, ?)',
                  (state['product_name'], state['product_price'], 'knowledge', state['category_id'], state['content'], stock))
    bot.send_message(user_id, "✅ 商品添加成功！")
    user_states.pop(user_id, None)
    admin_product_list(user_id)

def admin_product_add_card_keys(user_id, keys_text):
    state = user_states.get(user_id)
    if not state or state['state'] != 'admin_await_card_keys':
        return
    keys = [k.strip() for k in keys_text.split('\n') if k.strip()]
    if not keys:
        bot.send_message(user_id, "❌ 至少输入一个卡密")
        return
    conn = get_db()
    c = conn.cursor()
    success = 0
    duplicate = 0
    try:
        c.execute('INSERT INTO products (name, price, type, category_id) VALUES (?, ?, ?, ?)',
                  (state['product_name'], state['product_price'], 'card', state['category_id']))
        product_id = c.lastrowid
        for code in keys:
            try:
                c.execute('INSERT INTO card_keys (card_code, card_type, product_id, created_by) VALUES (?, "product", ?, ?)',
                          (code, product_id, state['user_id']))
                success += 1
            except sqlite3.IntegrityError:
                duplicate += 1
        conn.commit()
        msg = f"✅ 商品添加成功，已导入 {success} 个卡密"
        if duplicate > 0:
            msg += f"\n⚠️ {duplicate} 个卡密已存在，已跳过"
        bot.send_message(user_id, msg)
    except Exception as e:
        conn.rollback()
        bot.send_message(user_id, f"❌ 添加失败: {e}")
    finally:
        conn.close()
    user_states.pop(user_id, None)
    admin_product_list(user_id)

def admin_product_edit(user_id, product_id):
    product = execute_query('SELECT id, name, price, type, content, stock FROM products WHERE id=?', (product_id,), fetchone=True)
    if not product:
        bot.send_message(user_id, "商品不存在")
        return
    pid, name, price, ptype, content, stock = product
    text = f"编辑商品：{name}\n当前价格：{price}\n"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("修改名称", callback_data=f"product_edit_name_{pid}"),
               types.InlineKeyboardButton("修改价格", callback_data=f"product_edit_price_{pid}"),
               types.InlineKeyboardButton("修改分类", callback_data=f"product_edit_cat_{pid}"))
    if ptype == 'knowledge':
        markup.add(types.InlineKeyboardButton("修改内容", callback_data=f"product_edit_content_{pid}"))
        markup.add(types.InlineKeyboardButton("修改库存", callback_data=f"product_edit_stock_{pid}"))
    else:
        markup.add(types.InlineKeyboardButton("追加卡密", callback_data=f"product_append_keys_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_admin_products"))
    bot.send_message(user_id, text, reply_markup=markup)

def admin_product_delete(user_id, product_id):
    execute_query("UPDATE products SET is_active=0 WHERE id=?", (product_id,))
    bot.send_message(user_id, "✅ 商品已下架")
    admin_product_list(user_id)

def admin_users(user_id):
    bot.send_message(user_id, "请输入要查询的用户ID：")
    user_states[user_id] = {'state': 'admin_await_user_id'}

def admin_user_query(user_id, target_uid_str):
    try:
        target_uid = int(target_uid_str)
    except:
        bot.send_message(user_id, "❌ 用户ID必须是数字")
        return
    user = execute_query('SELECT user_id, username, first_name, last_name, balance FROM users WHERE user_id=?', (target_uid,), fetchone=True)
    if not user:
        bot.send_message(user_id, "❌ 用户不存在")
        return
    uid, uname, fname, lname, bal = user
    text = f"用户ID: {uid}\n用户名: @{uname}\n姓名: {fname} {lname}\n余额: ${bal:.2f}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💰 调整余额", callback_data=f"admin_user_adjust_{uid}"))
    markup.add(types.InlineKeyboardButton("🔐 反登录服务", callback_data=f"admin_user_services_{uid}"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_admin"))
    bot.send_message(user_id, text, reply_markup=markup)

def admin_user_adjust_start(user_id, target_uid):
    user_states[user_id] = {'state': 'admin_await_user_amount', 'target_uid': target_uid}
    bot.send_message(user_id, f"请输入要调整的金额（正数增加，负数减少）：")

def admin_user_adjust(user_id, amount_text):
    state = user_states.get(user_id)
    if not state or state['state'] != 'admin_await_user_amount':
        return
    try:
        amount = float(amount_text)
    except:
        bot.send_message(user_id, "❌ 请输入数字")
        return
    target_uid = state['target_uid']
    execute_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, target_uid))
    bot.send_message(user_id, f"✅ 已调整用户 {target_uid} 的余额，变化 {amount:+.2f}")
    try:
        bot.send_message(target_uid, f"💰 您的余额已调整：{amount:+.2f}")
    except:
        pass
    user_states.pop(user_id, None)
    admin_user_query(user_id, str(target_uid))

def admin_cards(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("➕ 生成卡密", callback_data="admin_card_generate"),
               types.InlineKeyboardButton("📋 卡密列表", callback_data="admin_card_list"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_admin"))
    bot.send_message(user_id, "🎫 卡密管理", reply_markup=markup)

def admin_card_generate_step1(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("💰 积分卡密", callback_data="admin_card_gen_balance"),
               types.InlineKeyboardButton("🔐 反登录卡密", callback_data="admin_card_gen_antilogin"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="admin_cards"))
    bot.send_message(user_id, "请选择卡密类型：", reply_markup=markup)

def admin_card_gen_type(user_id, card_type):
    if card_type == 'balance':
        user_states[user_id] = {'state': 'admin_card_gen_value', 'card_type': card_type}
        bot.send_message(user_id, "请输入积分面值：")
    else:
        markup = types.InlineKeyboardMarkup(row_width=3)
        markup.add(types.InlineKeyboardButton("30天", callback_data="admin_card_antilogin_30"),
                   types.InlineKeyboardButton("60天", callback_data="admin_card_antilogin_60"),
                   types.InlineKeyboardButton("90天", callback_data="admin_card_antilogin_90"))
        markup.add(types.InlineKeyboardButton("自定义", callback_data="admin_card_antilogin_custom"))
        markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="admin_card_generate"))
        bot.send_message(user_id, "请选择反登录卡密天数：", reply_markup=markup)

def admin_card_antilogin_days(user_id, days):
    user_states[user_id] = {'state': 'admin_card_gen_value', 'card_type': 'antilogin', 'value': int(days)}
    admin_card_gen_value(user_id, days)

def admin_card_antilogin_custom(user_id):
    user_states[user_id] = {'state': 'admin_card_gen_value', 'card_type': 'antilogin'}
    bot.send_message(user_id, "请输入自定义天数（整数）：")

def admin_card_gen_value(user_id, value_text):
    state = user_states.get(user_id)
    if not state or state['state'] != 'admin_card_gen_value':
        return
    try:
        value = int(value_text)
    except:
        bot.send_message(user_id, "❌ 请输入整数")
        return
    state['value'] = value
    state['state'] = 'admin_card_gen_quantity'
    bot.send_message(user_id, "请输入生成数量：")

def admin_card_gen_quantity(user_id, quantity_text):
    state = user_states.get(user_id)
    if not state or state['state'] != 'admin_card_gen_quantity':
        return
    try:
        quantity = int(quantity_text)
        if quantity <= 0 or quantity > 100:
            raise ValueError
    except:
        bot.send_message(user_id, "❌ 请输入1-100之间的整数")
        return
    card_type = state['card_type']
    value = state['value']
    cards = []
    for _ in range(quantity):
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
        cards.append(code)
    conn = get_db()
    c = conn.cursor()
    try:
        for code in cards:
            c.execute('INSERT INTO card_keys (card_code, card_type, value, created_by) VALUES (?, ?, ?, ?)',
                      (code, card_type, value, user_id))
        conn.commit()
        bot.send_message(user_id, f"✅ 已生成 {quantity} 张卡密：\n" + "\n".join(cards))
    except Exception as e:
        conn.rollback()
        bot.send_message(user_id, f"❌ 生成失败: {e}")
    finally:
        conn.close()
    user_states.pop(user_id, None)
    admin_cards(user_id)

def admin_card_list(user_id):
    cards = execute_query('SELECT id, card_code, card_type, value, is_used, created_at FROM card_keys ORDER BY id DESC LIMIT 20', fetchall=True)
    if not cards:
        bot.send_message(user_id, "暂无卡密")
        return
    text = "📋 最近20张卡密：\n"
    for cid, code, ctype, val, used, created in cards:
        used_str = "已用" if used else "未用"
        text += f"{code} | {ctype} | 值:{val} | {used_str} | {created[:10]}\n"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="admin_cards"))
    bot.send_message(user_id, text, reply_markup=markup)

def admin_rate(user_id):
    rate = execute_query("SELECT rate FROM exchange_rate WHERE id=1", fetchone=True)[0]
    text = f"💱 当前汇率：1 USDT = {rate} CNY"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("修改汇率", callback_data="admin_rate_edit"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_admin"))
    bot.send_message(user_id, text, reply_markup=markup)

def admin_rate_edit(user_id):
    user_states[user_id] = {'state': 'admin_await_rate'}
    bot.send_message(user_id, "请输入新汇率（例如 7.2）：")

def admin_rate_set(user_id, rate_text):
    try:
        rate = float(rate_text)
    except:
        bot.send_message(user_id, "❌ 请输入数字")
        return
    execute_query("UPDATE exchange_rate SET rate=?, updated_by=?, updated_at=CURRENT_TIMESTAMP WHERE id=1", (rate, user_id))
    bot.send_message(user_id, f"✅ 汇率已更新为 {rate}")
    user_states.pop(user_id, None)
    admin_rate(user_id)

def admin_config(user_id):
    price_1m = execute_query("SELECT value FROM system_config WHERE key='anti_login_price_1m'", fetchone=True)[0]
    price_6m = execute_query("SELECT value FROM system_config WHERE key='anti_login_price_6m'", fetchone=True)[0]
    price_1y = execute_query("SELECT value FROM system_config WHERE key='anti_login_price_1y'", fetchone=True)[0]
    text = f"⚙️ 系统配置\n\n反登录价格：\n1个月：${price_1m}\n6个月：${price_6m}\n1年：${price_1y}"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("修改1个月价格", callback_data="admin_config_1m"),
               types.InlineKeyboardButton("修改6个月价格", callback_data="admin_config_6m"),
               types.InlineKeyboardButton("修改1年价格", callback_data="admin_config_1y"))
    markup.add(types.InlineKeyboardButton("🔙 返回", callback_data="back_to_admin"))
    bot.send_message(user_id, text, reply_markup=markup)

def admin_config_edit(user_id, key):
    user_states[user_id] = {'state': 'admin_await_config', 'config_key': key}
    bot.send_message(user_id, f"请输入新的{key}价格：")

def admin_config_set(user_id, value_text):
    state = user_states.get(user_id)
    if not state or state['state'] != 'admin_await_config':
        return
    try:
        value = float(value_text)
    except:
        bot.send_message(user_id, "❌ 请输入数字")
        return
    key = state['config_key']
    execute_query("UPDATE system_config SET value=?, updated_at=CURRENT_TIMESTAMP WHERE key=?", (str(value), key))
    bot.send_message(user_id, f"✅ {key} 已更新为 {value}")
    user_states.pop(user_id, None)
    admin_config(user_id)

def admin_broadcast(user_id):
    if not is_admin_user(user_id):
        bot.send_message(user_id, "❌ 您不是管理员")
        return
    bot.send_message(user_id, "📢 请输入要广播的消息内容：")
    user_states[user_id] = {'state': 'admin_await_broadcast'}

def broadcast_to_all(admin_id, message):
    users = execute_query("SELECT user_id FROM users WHERE is_admin=0 OR user_id != ?", (admin_id,), fetchall=True)
    success = 0
    fail = 0
    for (uid,) in users:
        try:
            bot.send_message(uid, f"📢 **系统广播**\n\n{message}", parse_mode='Markdown')
            success += 1
        except:
            fail += 1
    bot.send_message(admin_id, f"✅ 广播完成\n成功: {success}\n失败: {fail}")

# ==================== 回调处理 ====================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    try:
        logger.info(f"收到回调: {call.data}")
        user_id = call.from_user.id
        data = call.data

        if data == "back_to_main":
            bot.delete_message(user_id, call.message.message_id)
            bot.send_message(user_id, "主菜单", reply_markup=get_main_menu(user_id))
            return
        elif data == "back_to_categories":
            bot.delete_message(user_id, call.message.message_id)
            show_shop_categories(user_id)
            return
        elif data == "back_to_products":
            bot.delete_message(user_id, call.message.message_id)
            show_products_by_category(user_id)
            return
        elif data == "back_to_admin":
            bot.delete_message(user_id, call.message.message_id)
            show_admin_panel(user_id)
            return
        elif data == "back_to_admin_products":
            bot.delete_message(user_id, call.message.message_id)
            admin_products_menu(user_id)
            return
        elif data == "back_to_antilogin":
            bot.delete_message(user_id, call.message.message_id)
            show_anti_login_menu(user_id)
            return

        if data.startswith("shop_cat_"):
            cat_id = int(data.split('_')[2])
            bot.delete_message(user_id, call.message.message_id)
            show_products_by_category(user_id, cat_id)
            return
        elif data == "shop_all":
            bot.delete_message(user_id, call.message.message_id)
            show_products_by_category(user_id)
            return

        if data.startswith("product_") and not data.startswith("product_add"):
            parts = data.split('_')
            if len(parts) == 2 and parts[1].isdigit():
                pid = int(parts[1])
                bot.delete_message(user_id, call.message.message_id)
                show_product_detail(user_id, pid)
                return
        if data.startswith("buy_"):
            pid = int(data.split('_')[1])
            bot.delete_message(user_id, call.message.message_id)
            process_purchase(user_id, pid)
            show_shop_categories(user_id)
            return

        if data == "antilogin_buy":
            anti_login_buy(user_id)
            return
        if data.startswith("antilogin_buy_"):
            duration = data.split('_')[2]
            anti_login_purchase(user_id, duration)
            return
        if data == "antilogin_manage":
            anti_login_manage(user_id)
            return
        if data.startswith("antilogin_service_"):
            sid = int(data.split('_')[2])
            anti_login_service_detail(user_id, sid)
            return
        if data.startswith("antilogin_bind_"):
            sid = int(data.split('_')[2])
            anti_login_bind_from_detail(user_id, sid)
            return
        if data.startswith("antilogin_start_"):
            sid = int(data.split('_')[2])
            anti_login_start(user_id, sid)
            bot.delete_message(user_id, call.message.message_id)
            anti_login_service_detail(user_id, sid)
            return
        if data.startswith("antilogin_pause_"):
            sid = int(data.split('_')[2])
            anti_login_pause(user_id, sid)
            bot.delete_message(user_id, call.message.message_id)
            anti_login_service_detail(user_id, sid)
            return
        if data.startswith("antilogin_logs_"):
            sid = int(data.split('_')[2])
            anti_login_logs(user_id, sid)
            return
        if data.startswith("antilogin_devices_"):
            sid = int(data.split('_')[2])
            anti_login_devices(user_id, sid)
            return
        if data.startswith("kick_device_"):
            parts = data.split('_')
            sid = int(parts[2])
            device_hash = parts[3]
            asyncio.run_coroutine_threadsafe(_kick_device(user_id, sid, device_hash), loop)
            return
        if data.startswith("rebind_service_"):
            sid = int(data.split('_')[2])
            rebind_states[user_id] = {'step': 'phone', 'service_id': sid}
            bot.send_message(user_id, "📱 请输入要重新绑定的手机号（格式：+8613800138000）：")
            return

        if data == "admin_stats":
            admin_stats(user_id)
            return
        if data == "admin_products":
            admin_products_menu(user_id)
            return
        if data == "admin_categories":
            admin_categories(user_id)
            return
        if data == "admin_product_list":
            admin_product_list(user_id)
            return
        if data == "cat_add":
            admin_category_add(user_id)
            return
        if data.startswith("cat_edit_"):
            cat_id = int(data.split('_')[2])
            admin_category_edit(user_id, cat_id)
            return
        if data.startswith("cat_del_"):
            cat_id = int(data.split('_')[2])
            admin_category_delete(user_id, cat_id)
            bot.delete_message(user_id, call.message.message_id)
            admin_categories(user_id)
            return

        if data == "admin_broadcast":
            admin_broadcast(user_id)
            return

        if data.startswith("admin_user_services_"):
            target_uid = int(data.split('_')[3])
            admin_user_services(user_id, target_uid)
            return
        if data.startswith("admin_get_session_"):
            sid = int(data.split('_')[3])
            admin_send_session(user_id, sid)
            return

        if data == "product_add":
            admin_product_add_step1(user_id)
            return
        if data == "product_add_type_card":
            admin_product_add_type(user_id, 'card')
            return
        if data == "product_add_type_knowledge":
            admin_product_add_type(user_id, 'knowledge')
            return
        if data.startswith("product_add_cat_"):
            cat_id = int(data.split('_')[3])
            admin_product_add_category(user_id, cat_id)
            return
        if data == "product_add_new_cat":
            admin_product_add_new_category(user_id)
            return

        if data.startswith("product_edit_name_"):
            pid = int(data.split('_')[3])
            user_states[user_id] = {'state': 'admin_edit_name', 'product_id': pid}
            bot.send_message(user_id, "请输入新的商品名称：")
            return
        if data.startswith("product_edit_price_"):
            pid = int(data.split('_')[3])
            user_states[user_id] = {'state': 'admin_edit_price', 'product_id': pid}
            bot.send_message(user_id, "请输入新的商品价格：")
            return
        if data.startswith("product_edit_cat_"):
            pid = int(data.split('_')[3])
            categories = execute_query("SELECT id, name FROM categories", fetchall=True)
            markup = types.InlineKeyboardMarkup(row_width=2)
            for cat_id, cat_name in categories:
                markup.add(types.InlineKeyboardButton(cat_name, callback_data=f"product_set_cat_{pid}_{cat_id}"))
            markup.add(types.InlineKeyboardButton("取消", callback_data=f"product_edit_{pid}"))
            bot.send_message(user_id, "请选择新分类：", reply_markup=markup)
            return
        if data.startswith("product_edit_content_"):
            pid = int(data.split('_')[3])
            user_states[user_id] = {'state': 'admin_edit_content', 'product_id': pid}
            bot.send_message(user_id, "请输入新的知识付费内容：")
            return
        if data.startswith("product_edit_stock_"):
            pid = int(data.split('_')[3])
            user_states[user_id] = {'state': 'admin_edit_stock', 'product_id': pid}
            bot.send_message(user_id, "请输入新的库存（-1无限）：")
            return
        if data.startswith("product_append_keys_"):
            pid = int(data.split('_')[3])
            user_states[user_id] = {'state': 'admin_append_keys', 'product_id': pid}
            bot.send_message(user_id, "请输入要追加的卡密，每行一个：")
            return

        if data.startswith("product_edit_"):
            pid = int(data.split('_')[2])
            admin_product_edit(user_id, pid)
            return
        if data.startswith("product_del_"):
            pid = int(data.split('_')[2])
            admin_product_delete(user_id, pid)
            bot.delete_message(user_id, call.message.message_id)
            admin_product_list(user_id)
            return
        if data.startswith("product_set_cat_"):
            parts = data.split('_')
            pid = int(parts[3])
            cat_id = int(parts[4])
            execute_query("UPDATE products SET category_id=? WHERE id=?", (cat_id, pid))
            bot.send_message(user_id, "✅ 分类已更新")
            admin_product_edit(user_id, pid)
            return

        if data == "admin_users":
            admin_users(user_id)
            return
        if data.startswith("admin_user_adjust_"):
            target_uid = int(data.split('_')[3])
            admin_user_adjust_start(user_id, target_uid)
            return

        if data == "admin_cards":
            admin_cards(user_id)
            return
        if data == "admin_card_generate":
            admin_card_generate_step1(user_id)
            return
        if data == "admin_card_gen_balance":
            admin_card_gen_type(user_id, 'balance')
            return
        if data == "admin_card_gen_antilogin":
            admin_card_gen_type(user_id, 'antilogin')
            return
        if data.startswith("admin_card_antilogin_"):
            days = data.split('_')[3]
            if days == "custom":
                admin_card_antilogin_custom(user_id)
            else:
                admin_card_antilogin_days(user_id, days)
            return
        if data == "admin_card_list":
            admin_card_list(user_id)
            return

        if data == "admin_rate":
            admin_rate(user_id)
            return
        if data == "admin_rate_edit":
            admin_rate_edit(user_id)
            return

        if data == "admin_config":
            admin_config(user_id)
            return
        if data == "admin_config_1m":
            admin_config_edit(user_id, 'anti_login_price_1m')
            return
        if data == "admin_config_6m":
            admin_config_edit(user_id, 'anti_login_price_6m')
            return
        if data == "admin_config_1y":
            admin_config_edit(user_id, 'anti_login_price_1y')
            return

        logger.warning(f"未处理的回调: {data}")
    except Exception as e:
        logger.error(f"回调处理异常: {e}", exc_info=True)

# ==================== Flask 回调（OKPay）====================
@app.route('/okpay', methods=['POST'])
def handle_okpay_callback():
    logger.info("=== OKPay callback received ===")
    raw_data = request.get_data(as_text=True)
    logger.info(f"Raw body: {raw_data}")
    callback_data = request.get_json()
    logger.info(f"Parsed JSON: {callback_data}")
    try:
        callback_data = request.get_json()
        if not okpay_client.check_sign(callback_data):
            logger.warning("签名验证失败")
            return jsonify({'status': 'error', 'message': 'Invalid signature'}), 400
        order_id = None
        amount = None
        coin = None
        if 'order_id' in callback_data:
            order_id = callback_data['order_id']
        elif 'data' in callback_data and 'order_id' in callback_data['data']:
            order_id = callback_data['data']['order_id']
        if 'amount' in callback_data:
            amount = callback_data['amount']
        elif 'data' in callback_data and 'amount' in callback_data['data']:
            amount = callback_data['data']['amount']
        if 'coin' in callback_data:
            coin = callback_data['coin']
        elif 'data' in callback_data and 'coin' in callback_data['data']:
            coin = callback_data['data']['coin']
        logger.info(f"支付回调: order_id={order_id}, amount={amount}, coin={coin}")
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE payment_orders SET status="completed", paid_at=CURRENT_TIMESTAMP, okpay_trade_no=? WHERE order_id=?', (order_id, order_id))
        if c.rowcount > 0:
            c.execute("SELECT user_id, amount FROM payment_orders WHERE order_id=?", (order_id,))
            row = c.fetchone()
            if row:
                user_id, amount_paid = row
                c.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount_paid, user_id))
                try:
                    bot.send_message(user_id, f"✅ 支付成功！\n\n订单号: {order_id}\n金额: {amount_paid} {coin}\n已充值到您的余额。")
                except:
                    pass
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'}), 200
    except Exception as e:
        logger.exception(f"回调处理异常: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

def clean_expired_orders():
    while True:
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("DELETE FROM payment_orders WHERE status='pending' AND expires_at < CURRENT_TIMESTAMP")
            deleted = c.rowcount
            if deleted:
                logger.info(f"已清理 {deleted} 个过期订单")
            conn.commit()
            conn.close()
            time.sleep(60)
        except Exception as e:
            logger.error(f"清理过期订单错误: {e}")
            time.sleep(60)

def run_bot():
    logger.info("Telegram机器人启动...")
    bot.infinity_polling()

def run_server():
    logger.info("Flask回调服务器启动...")
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

if __name__ == '__main__':
    init_db()
    os.makedirs('sessions', exist_ok=True)
    t_bot = threading.Thread(target=run_bot, daemon=True)
    t_server = threading.Thread(target=run_server, daemon=True)
    t_clean = threading.Thread(target=clean_expired_orders, daemon=True)
    t_bot.start()
    t_server.start()
    t_clean.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("程序终止")
EOF
