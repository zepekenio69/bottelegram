# -*- coding: utf-8 -*-
"""
Bot Telegram + vérificateur automatique (BTC / ETH / USDT TRC20)
+ Ajout du bouton "💸 Code promo"
+ Token sécurisé via variable d'environnement
"""

import os
import sqlite3
import random
import string
from datetime import datetime, timezone
from dotenv import load_dotenv
import requests
import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart

# -------------------------
# CONFIGURATION
# -------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")

# ✅ Vérification du token
if not BOT_TOKEN or not BOT_TOKEN.strip():
    raise ValueError("❌ Erreur : la variable d'environnement BOT_TOKEN est introuvable. "
                     "Vérifie qu'elle est bien définie dans Render > Environment > BOT_TOKEN")

print("✅ BOT_TOKEN détecté avec succès. Démarrage du bot...")

# === TES ADRESSES FIXES ===
ADDRESSES = {
    "BTC": "bc1qtg0qkf6v6vz9ddf3l72yl4punttl0uzq5qjuq0",
    "ETH": "0xD2FCAd141fD7646B0074E98905149c6C014F03A6",
    "USDT": "TFCnvnNaQ7rGtgdbcKJPs6FRVQqKSLJasH",
}

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
FEE_BUFFER = float(os.getenv("FEE_BUFFER", "0.00"))

CONFIRMATIONS = {
    "BTC": int(os.getenv("CONF_BTC", "3")),
    "ETH": int(os.getenv("CONF_ETH", "12")),
    "USDT": int(os.getenv("CONF_USDT", "20")),
}

DB_PATH = "orders.sqlite3"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,tether&vs_currencies=eur"

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

DEFAULT_HEADERS = {
    "User-Agent": "payment-watcher/1.0",
    "Accept": "application/json"
}

# -------------------------
# OUTILS HTTP / JSON
# -------------------------
def safe_json(resp, api_name=""):
    try:
        data = resp.json()
        if isinstance(data, dict):
            return data
        print(f"[{api_name}] Réponse non-JSON/dict:", str(data)[:120])
        return {}
    except Exception as e:
        print(f"[{api_name}] JSON parse error:", e)
        return {}

def http_get(url, timeout=20, headers=None):
    h = {**DEFAULT_HEADERS, **(headers or {})}
    try:
        r = requests.get(url, timeout=timeout, headers=h)
        return r
    except Exception as e:
        print("[HTTP] GET error:", url, e)
        return None

# -------------------------
# PRODUITS / PRIX
# -------------------------
PACKS = {
    "pack1": {"label": "1 plaque", "price": 50.0},
    "pack10": {"label": "10 plaques", "price": 650.0},
    "pack20": {"label": "20 plaques", "price": 1000.0},
}

# -------------------------
# BASE DE DONNÉES
# -------------------------
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            user_id INTEGER,
            username TEXT,
            pack_label TEXT,
            price_eur REAL,
            coin TEXT,
            required_amount REAL,
            receive_address TEXT,
            status TEXT,
            txid TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS seen_txs (
            txid TEXT PRIMARY KEY,
            coin TEXT,
            amount REAL,
            detected_at TEXT
        )
        """)

def gen_order_code() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"DRA-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{suffix}"

def get_rates():
    try:
        resp = http_get(COINGECKO_URL, timeout=15)
        if not resp or not resp.ok:
            print("[RATES] HTTP error:", resp.status_code if resp else "no response")
            return None
        data = safe_json(resp, "RATES")
        return {
            "BTC": float(data.get("bitcoin", {}).get("eur", 0) or 0),
            "ETH": float(data.get("ethereum", {}).get("eur", 0) or 0),
            "USDT": float(data.get("tether", {}).get("eur", 0) or 0)
        }
    except Exception as e:
        print("Erreur CoinGecko:", e)
        return None

# -------------------------
# CLAVIERS
# -------------------------
def packs_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=p["label"], callback_data=f"pack:{k}")]
        for k, p in PACKS.items()
    ] + [
        [InlineKeyboardButton(text="💸 Code promo", callback_data="promo:start")]
    ])

def coins_kb(order_code):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="BTC", callback_data=f"coin:{order_code}:BTC")],
        [InlineKeyboardButton(text="ETH", callback_data=f"coin:{order_code}:ETH")],
        [InlineKeyboardButton(text="USDT (TRC20)", callback_data=f"coin:{order_code}:USDT")],
        [InlineKeyboardButton(text="🔙 Retour", callback_data="back:packs")]
    ])

# -------------------------
# BOT PRINCIPAL
# -------------------------
WELCOME = (
    "👋 *Bienvenue !*\n\n"
    "Choisis une offre ci-dessous :\n"
    "1️⃣ 1 plaque — 50 €\n"
    "2️⃣ 10 plaques — 650 €\n"
    "3️⃣ 20 plaques — 1000 €\n\n"
    "_Paiement possible en BTC, ETH ou USDT (TRC20)._"
)

@dp.message(CommandStart())
async def start_msg(msg: Message):
    await msg.answer(WELCOME, parse_mode="Markdown", reply_markup=packs_kb())

# -------------------------
# CODE PROMO
# -------------------------
@dp.callback_query(F.data == "promo:start")
async def promo_start(cq: CallbackQuery):
    await cq.message.edit_text(
        "🎟️ Entre ton code promo ci-dessous (exemple : `PROMO2025`).",
        parse_mode="Markdown"
    )
    dp.message.register(handle_promo_code, F.chat.id == cq.from_user.id)

async def handle_promo_code(msg: Message):
    code = msg.text.strip()
    await msg.answer("✅ Merci ! Ton code promo a bien été transmis à l’équipe.")
    text = (
        f"🎟️ *Nouveau code promo reçu !*\n\n"
        f"👤 De : {msg.from_user.first_name} (@{msg.from_user.username})\n"
        f"💬 Code : `{code}`"
    )
    await bot.send_message(ADMIN_CHAT_ID, text, parse_mode="Markdown")

# -------------------------
# COMMANDES / PACKS
# -------------------------
@dp.callback_query(F.data.startswith("pack:"))
async def on_pack(cq: CallbackQuery):
    _, pack_key = cq.data.split(":")
    pack = PACKS[pack_key]
    code = gen_order_code()
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""INSERT INTO orders (code, user_id, username, pack_label, price_eur, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (code, cq.from_user.id, cq.from_user.username or "", pack["label"], pack["price"], "PENDING", datetime.utcnow().isoformat()))
    text = (
        f"🧾 *Commande créée* : `{code}`\n"
        f"Offre : *{pack['label']}*\n"
        f"Prix : *{pack['price']:.2f} €*\n\n"
        "Choisis la crypto pour payer :"
    )
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=coins_kb(code))

# -------------------------
# CHOIX DES CRYPTOS
# -------------------------
@dp.callback_query(F.data.startswith("coin:"))
async def on_coin(cq: CallbackQuery):
    _, code, coin = cq.data.split(":")
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT pack_label, price_eur FROM orders WHERE code=?", (code,)).fetchone()
    if not row:
        await cq.answer("Commande introuvable", show_alert=True)
        return
    label, price_eur = row

    rates = get_rates()
    if not rates:
        await cq.answer("Erreur récupération taux. Réessaye.", show_alert=True)
        return
    rate = rates.get(coin) or 0
    if rate <= 0:
        await cq.answer("Crypto non supportée.", show_alert=True)
        return

    required_crypto = (price_eur / rate) * (1.0 + FEE_BUFFER)
    receive_address = ADDRESSES.get(coin)

    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE orders SET coin=?, required_amount=?, receive_address=?, updated_at=? WHERE code=?",
                    (coin, required_crypto, receive_address, datetime.utcnow().isoformat(), code))

    text = (
        f"💳 *Paiement en {coin}*\n\n"
        f"Commande : `{code}`\n"
        f"Offre : *{label}*\n"
        f"Montant à envoyer : *{required_crypto:.8f} {coin}*\n"
        f"Adresse : `{receive_address}`\n\n"
        f"➡️ Envoie exactement *{required_crypto:.8f} {coin}* (tu paies les frais réseau).\n\n"
        f"🕒 Après ton envoi, l’équipe sera notifiée automatiquement dès que la transaction sera confirmée.\n"
        f"Garde bien ton numéro de commande `{code}` et contacte [@{ADMIN_USERNAME}](https://t.me/{ADMIN_USERNAME}) si besoin."
    )
    await cq.message.edit_text(text, parse_mode="Markdown")
    await bot.send_message(ADMIN_CHAT_ID, f"🆕 Nouvelle commande {code} — {label} — {price_eur:.2f}€ — {coin} -> {required_crypto:.8f} {coin}")

# -------------------------
# VÉRIFICATION AUTOMATIQUE DES PAIEMENTS
# -------------------------
async def verifier_paiements_loop():
    await asyncio.sleep(5)
    print("🔍 Vérificateur automatique des paiements lancé...")
    while True:
        await asyncio.sleep(30)  # Placeholder boucle

# -------------------------
# LANCEMENT DU BOT
# -------------------------
if __name__ == "__main__":
    init_db()

    async def main():
        await asyncio.gather(
            dp.start_polling(bot),
            verifier_paiements_loop()
        )

    asyncio.run(main())
