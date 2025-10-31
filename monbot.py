# -*- coding: utf-8 -*-
"""
Bot Telegram + vérificateur automatique (BTC / ETH / USDT TRC20)
- Défensif sur les APIs (BTC: BlockCypher, ETH: Etherscan, USDT: TronGrid/TronScan)
- Notification UNIQUEMENT à l'admin quand la transaction est confirmée
- Adresses fixes fournies
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
# CONFIG
# -------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "alberber27")

# === TES ADRESSES FIXES ===
# BTC, ETH, USDT(TRC20 - Tron)
ADDRESSES = {
    "BTC": "bc1qtg0qkf6v6vz9ddf3l72yl4punttl0uzq5qjuq0",
    "ETH": "0xD2FCAd141fD7646B0074E98905149c6C014F03A6",
    "USDT": "TFCnvnNaQ7rGtgdbcKJPs6FRVQqKSLJasH",  # USDT TRC20 (Tron)
}

# API keys optionnelles (améliorent la fiabilité)
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")

# Le client paie les frais réseau (buffer=0.00). Tu peux mettre 0.01~0.03 si tu veux une marge.
FEE_BUFFER = float(os.getenv("FEE_BUFFER", "0.00"))

# Confirmations minimales (tu peux réduire pour des tests)
CONFIRMATIONS = {
    "BTC": int(os.getenv("CONF_BTC", "3")),
    "ETH": int(os.getenv("CONF_ETH", "12")),
    "USDT": int(os.getenv("CONF_USDT", "20")),
}

DB_PATH = "orders.sqlite3"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,tether&vs_currencies=eur"

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# -------------------------
# HEADERS / HELPERS HTTP
# -------------------------
DEFAULT_HEADERS = {
    "User-Agent": "payment-watcher/1.0",
    "Accept": "application/json"
}

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
# DB & utilitaires
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
# Keyboards
# -------------------------
def packs_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=p["label"], callback_data=f"pack:{k}")]
        for k, p in PACKS.items()
    ])

def coins_kb(order_code):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="BTC", callback_data=f"coin:{order_code}:BTC")],
        [InlineKeyboardButton(text="ETH", callback_data=f"coin:{order_code}:ETH")],
        [InlineKeyboardButton(text="USDT (TRC20)", callback_data=f"coin:{order_code}:USDT")],
        [InlineKeyboardButton(text="🔙 Retour", callback_data="back:packs")]
    ])

# -------------------------
# BOT
# -------------------------
WELCOME = (
    "👋 *Bienvenue* — choisis une offre :\n\n"
    "1️⃣ 1 plaque — 50 €\n"
    "2️⃣ 10 plaques — 650 €\n"
    "3️⃣ 20 plaques — 1000 €\n\n"
    "_Paiement possible en BTC, ETH ou USDT (TRC20)._"
)

@dp.message(CommandStart())
async def start_msg(msg: Message):
    await msg.answer(WELCOME, parse_mode="Markdown", reply_markup=packs_kb())

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
        f"🕒 Après ton envoi, c’est *l’équipe* qui recevra la confirmation automatique dès que la transaction sera validée.\n"
        f"Garde bien ton numéro de commande `{code}` et contacte [@{ADMIN_USERNAME}](https://t.me/{ADMIN_USERNAME}) si besoin."
    )
    await cq.message.edit_text(text, parse_mode="Markdown")
    await bot.send_message(ADMIN_CHAT_ID, f"🆕 Nouvelle commande {code} — {label} — {price_eur:.2f}€ — {coin} -> {required_crypto:.8f} {coin}")

# -------------------------
# SURVEILLANCE AUTOMATIQUE
# -------------------------
def mark_tx_seen(txid, coin, amount):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR IGNORE INTO seen_txs (txid, coin, amount, detected_at) VALUES (?, ?, ?, ?)",
                    (txid, coin, amount, datetime.utcnow().isoformat()))

def tx_already_seen(txid):
    with sqlite3.connect(DB_PATH) as con:
        r = con.execute("SELECT 1 FROM seen_txs WHERE txid=?", (txid,)).fetchone()
    return r is not None

def find_matching_order_for_tx(coin, to_address, amount):
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("""
            SELECT code, required_amount
            FROM orders
            WHERE status='PENDING' AND coin=? AND receive_address=?
        """, (coin, to_address)).fetchall()
    for code, required in rows:
        if amount >= (required * 0.999):  # petite marge
            return code
    return None

async def verifier_paiements_loop():
    await asyncio.sleep(5)
    print("🔍 Vérificateur automatique des paiements lancé...")
    while True:
        try:
            # Liste des couples (coin, address) encore en attente
            with sqlite3.connect(DB_PATH) as con:
                pending = con.execute("""
                    SELECT DISTINCT coin, receive_address
                    FROM orders
                    WHERE status='PENDING' AND receive_address IS NOT NULL
                """).fetchall()

            for coin, addr in pending:
                if not addr:
                    continue

                # -------- BTC (BlockCypher) --------
                if coin == "BTC":
                    url = f"https://api.blockcypher.com/v1/btc/main/addrs/{addr}?limit=50"
                    resp = http_get(url)
                    if not resp or not resp.ok:
                        print("[BTC] HTTP error:", resp.status_code if resp else "no response")
                        continue
                    data = safe_json(resp, "BTC")
                    txrefs = data.get("txrefs") or []
                    unconf = data.get("unconfirmed_txrefs") or []
                    for tx in (txrefs + unconf):
                        if not isinstance(tx, dict):
                            continue
                        txid = tx.get("tx_hash")
                        if not txid or tx_already_seen(txid):
                            continue
                        try:
                            confirmations = int(tx.get("confirmations", 0) or 0)
                        except:
                            confirmations = 0
                        if confirmations < CONFIRMATIONS["BTC"]:
                            continue
                        try:
                            amount_btc = (int(tx.get("value", 0) or 0)) / 1e8
                        except:
                            continue
                        code = find_matching_order_for_tx("BTC", addr, amount_btc)
                        if code:
                            with sqlite3.connect(DB_PATH) as con:
                                con.execute(
                                    "UPDATE orders SET status='PAID', txid=?, updated_at=? WHERE code=?",
                                    (txid, datetime.utcnow().isoformat(), code)
                                )
                            await bot.send_message(
                                ADMIN_CHAT_ID,
                                f"✅ Paiement BTC confirmé pour {code}\nTx: `{txid}`\nMontant: {amount_btc:.8f} BTC",
                                parse_mode="Markdown"
                            )
                            mark_tx_seen(txid, "BTC", amount_btc)

                # -------- ETH (Etherscan) --------
                elif coin == "ETH":
                    base = f"https://api.etherscan.io/api?module=account&action=txlist&address={addr}&startblock=0&endblock=99999999&sort=desc"
                    if ETHERSCAN_API_KEY:
                        base += f"&apikey={ETHERSCAN_API_KEY}"
                    resp = http_get(base)
                    if not resp or not resp.ok:
                        print("[ETH] HTTP error:", resp.status_code if resp else "no response")
                        continue
                    data = safe_json(resp, "ETH")
                    result = data.get("result")
                    if not isinstance(result, list):
                        print("[ETH] result non-liste ou vide")
                        continue
                    for tx in result:
                        if not isinstance(tx, dict):
                            continue
                        txid = tx.get("hash")
                        if not txid or tx_already_seen(txid):
                            continue
                        to_addr = (tx.get("to") or "").lower()
                        if to_addr != addr.lower():
                            continue
                        try:
                            confirmations = int(tx.get("confirmations", 0) or 0)
                        except:
                            confirmations = 0
                        if confirmations < CONFIRMATIONS["ETH"]:
                            continue
                        try:
                            amount_eth = int(tx.get("value", 0) or 0) / 1e18
                        except:
                            continue
                        code = find_matching_order_for_tx("ETH", addr, amount_eth)
                        if code:
                            with sqlite3.connect(DB_PATH) as con:
                                con.execute(
                                    "UPDATE orders SET status='PAID', txid=?, updated_at=? WHERE code=?",
                                    (txid, datetime.utcnow().isoformat(), code)
                                )
                            await bot.send_message(
                                ADMIN_CHAT_ID,
                                f"✅ Paiement ETH confirmé pour {code}\nTx: `{txid}`\nMontant: {amount_eth:.8f} ETH",
                                parse_mode="Markdown"
                            )
                            mark_tx_seen(txid, "ETH", amount_eth)

                # -------- USDT TRC20 (TronGrid/TronScan) --------
                elif coin == "USDT":
                    headers = {}
                    if TRONGRID_API_KEY:
                        headers["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
                    url_trongrid = f"https://api.trongrid.io/v1/accounts/{addr}/transactions/trc20?only_to=true&limit=50&order_by=block_timestamp,desc"
                    resp = http_get(url_trongrid, headers=headers)
                    items = []
                    if resp and resp.ok:
                        data = safe_json(resp, "TRONGRID")
                        items = data.get("data") or []
                    else:
                        url_tronscan = f"https://apilist.tronscan.org/api/contract/events?count=true&limit=50&sort=-timestamp&contract=TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t&toAddress={addr}"
                        resp2 = http_get(url_tronscan)
                        if resp2 and resp2.ok:
                            data2 = safe_json(resp2, "TRONSCAN")
                            items = data2.get("data") or []

                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        txid = it.get("transaction_id") or it.get("transactionHash") or it.get("txID") or it.get("hash")
                        if not txid or tx_already_seen(txid):
                            continue
                        to_addr = (it.get("to") or it.get("toAddress") or "").upper()
                        if to_addr and to_addr != addr.upper():
                            continue
                        raw_val = it.get("value") or it.get("amount") or (it.get("tokenInfo") or {}).get("value")
                        try:
                            # certaines APIs renvoient une string d'entier (6 décimales pour USDT TRC20)
                            val = float(raw_val)
                        except:
                            try:
                                val = float(int(str(raw_val)))
                            except:
                                continue
                        amount_usdt = val / 1e6 if val > 1000 else val
                        code = find_matching_order_for_tx("USDT", addr, amount_usdt)
                        if code:
                            with sqlite3.connect(DB_PATH) as con:
                                con.execute(
                                    "UPDATE orders SET status='PAID', txid=?, updated_at=? WHERE code=?",
                                    (txid, datetime.utcnow().isoformat(), code)
                                )
                            await bot.send_message(
                                ADMIN_CHAT_ID,
                                f"✅ Paiement USDT(TRC20) confirmé pour {code}\nTx: `{txid}`\nMontant: {amount_usdt:.6f} USDT",
                                parse_mode="Markdown"
                            )
                            mark_tx_seen(txid, "USDT", amount_usdt)

            await asyncio.sleep(30)  # pause entre boucles (ajuste si besoin)
        except Exception as e:
            print("⚠️ Erreur (boucle principale):", e)
            await asyncio.sleep(30)

# -------------------------
# LANCEMENT
# -------------------------
if __name__ == "__main__":
    init_db()

    async def main():
        await asyncio.gather(
            dp.start_polling(bot),
            verifier_paiements_loop()
        )

    asyncio.run(main())
