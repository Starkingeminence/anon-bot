"""
routers/treasury.py
Phase 4 — Treasury & Payout Module

Exposes:
  • /treasury command   — streams live Multi-Sig balance + 1/12th projection
  • treasury_router     — aiogram Router (wire into build_dispatcher)

All heavy computation lives in tasks/payout_engine.py (see companion file).
This file is kept thin: it only handles the Telegram-facing read-only command.
"""

import logging
import os

import aiohttp
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

log = logging.getLogger(__name__)
router = Router()

# ---------------------------------------------------------------------------
# Config — load from environment (injected at startup)
# ---------------------------------------------------------------------------
DAO_GROUP_ID: int = int(os.environ.get("DAO_GROUP_ID", "-1001234567890"))
ACKI_NACKI_RPC_URL: str = os.environ.get("ACKI_NACKI_RPC_URL", "")
MULTISIG_WALLET: str = os.environ.get("MULTISIG_WALLET_ADDRESS", "")


# ---------------------------------------------------------------------------
# Helper: query blockchain balance via Acki Nacki GraphQL RPC
# ---------------------------------------------------------------------------
async def fetch_multisig_balance(session: aiohttp.ClientSession) -> float | None:
    """
    Queries the Acki Nacki GraphQL endpoint for the Multi-Sig wallet balance.
    Returns balance as a float (NACKL units) or None on failure.
    """
    query = """
    query GetBalance($address: String!) {
      blockchain {
        account(address: $address) {
          info {
            balance(format: DEC)
          }
        }
      }
    }
    """
    payload = {"query": query, "variables": {"address": MULTISIG_WALLET}}
    try:
        async with session.post(
            ACKI_NACKI_RPC_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                log.error("RPC non-200: %s", resp.status)
                return None
            data = await resp.json()
            raw = (
                data.get("data", {})
                    .get("blockchain", {})
                    .get("account", {})
                    .get("info", {})
                    .get("balance")
            )
            return float(raw) / 1e9 if raw else None  # nanotokens → NACKL
    except Exception as exc:
        log.error("RPC fetch failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# /treasury — streaming read-only command
# ---------------------------------------------------------------------------
@router.message(Command("treasury"), F.chat.id == DAO_GROUP_ID)
async def cmd_treasury(message: Message, bot: Bot, pool):
    """
    Streams the current Multi-Sig balance and 1/12th runway projection
    using Telegram's progressive message-edit pattern (streaming UX).
    """
    status_msg = await message.reply("🔍 Querying blockchain…")

    async with aiohttp.ClientSession() as session:
        balance = await fetch_multisig_balance(session)

    if balance is None:
        await status_msg.edit_text(
            "⚠️ Could not reach the Acki Nacki RPC endpoint. Try again shortly."
        )
        return

    monthly_pool = balance / 12.0
    founder_cut = monthly_pool * 0.01
    community_pool = monthly_pool * 0.99

    # Pull this month's total points from DB for context
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT SUM(points_monthly) AS total FROM users"
        )
        total_pts = row["total"] or 0

    # Stream output line-by-line via progressive edits
    lines = [
        "💰 *DAO Treasury Report*",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    await status_msg.edit_text("\n".join(lines), parse_mode="Markdown")

    lines.append(f"🏦 Multi-Sig Balance: `{balance:,.4f} NACKL`")
    await status_msg.edit_text("\n".join(lines), parse_mode="Markdown")

    lines.append(f"📅 Monthly Pool (1/12th): `{monthly_pool:,.4f} NACKL`")
    await status_msg.edit_text("\n".join(lines), parse_mode="Markdown")

    lines.append(f"👑 Founder Reserve (1%): `{founder_cut:,.4f} NACKL`")
    await status_msg.edit_text("\n".join(lines), parse_mode="Markdown")

    lines.append(f"🌐 Community Pool (99%): `{community_pool:,.4f} NACKL`")
    await status_msg.edit_text("\n".join(lines), parse_mode="Markdown")

    lines.append(f"📊 Active Monthly Points: `{total_pts:,}`")
    await status_msg.edit_text("\n".join(lines), parse_mode="Markdown")

    if total_pts > 0:
        nackl_per_point = community_pool / total_pts
        lines.append(f"⚡ Est. Value/Point: `{nackl_per_point:.6f} NACKL`")
        await status_msg.edit_text("\n".join(lines), parse_mode="Markdown")

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("_Figures are live. Point values shift as more members earn._")
    await status_msg.edit_text("\n".join(lines), parse_mode="Markdown")
