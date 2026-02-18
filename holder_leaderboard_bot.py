#!/usr/bin/env python3
"""
Discord bot: maintains a permanent donation leaderboard message.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import List, Tuple, Set, Optional, Dict

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from incoming_tracker import (
    IncomingTracker,
    HeliusRPCClient,
    USDC_MINT,
    USDT_MINT,
    SOL_DECIMALS,
    _init_transactions_db,
    _to_iso,
    _token_account_map,
    _iter_system_transfer_instructions,
    _iter_token_transfer_instructions,
    _parse_transfer,
    _ui_amount,
    _compute_values_for_rows,
    _init_transactions_db,
    _init_snapshot_db,
    _apply_donations,
    _ensure_otp_registry,
    _expire_otps,
    _format_otp_if_exact,
    _match_assigned_otp,
    _record_used_otp,
)

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("holder_leaderboard")

SNAPSHOT_DB = os.getenv("SNAPSHOT_DB", "/app/data/fartboy_snapshot.db")
SNAPSHOT_TABLE = os.getenv("SNAPSHOT_TABLE", "fartboy_holders")
STATE_DB = os.getenv("LEADERBOARD_STATE_DB", "/app/data/leaderboard_state.db")
UPDATE_SECONDS = int(os.getenv("LEADERBOARD_UPDATE_SECONDS", "300"))
DEFAULT_LIMIT = 30
GUILD_ID = os.getenv("DISCORD_GUILD_ID")
ENABLE_DM_COMMANDS = os.getenv("ENABLE_DM_COMMANDS", "").lower() in {"1", "true", "yes"}
SNAPSHOT_WATCH_SECONDS = int(os.getenv("SNAPSHOT_WATCH_SECONDS", "10"))
LEADERBOARD_REFRESH_COOLDOWN = int(os.getenv("LEADERBOARD_REFRESH_COOLDOWN_SECONDS", "10"))
TRACKER_INTERVAL_SECONDS = int(os.getenv("TRACKER_INTERVAL_SECONDS", "120"))
TX_DB = os.getenv("TX_DB", "/app/data/incoming_transactions.db")
TX_TABLE = os.getenv("TX_TABLE", "incoming_transactions")
TX_LOOKUP_LIMIT = int(os.getenv("TX_LOOKUP_LIMIT", "50"))
RPC_REQUEST_DELAY = float(os.getenv("RPC_REQUEST_DELAY", "0.1"))
SUMMARY_TABLE = os.getenv("SUMMARY_TABLE", "verified_users")
OTP_TABLE = os.getenv("OTP_TABLE", "otp_registry")
OTP_EXPIRY_SECONDS = int(os.getenv("OTP_EXPIRY_SECONDS", "3600"))
COMMAND_CHANNEL_ID = os.getenv("COMMAND_CHANNEL_ID")
_SNAPSHOT_COLUMNS: Set[str] | None = None


def _connect_db(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return sqlite3.connect(path)




def _ensure_snapshot_schema() -> None:
    global _SNAPSHOT_COLUMNS
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({SNAPSHOT_TABLE})")}
            if "discord_id" not in existing:
                conn.execute(f"ALTER TABLE {SNAPSHOT_TABLE} ADD COLUMN discord_id TEXT")
            if "discord_name" not in existing:
                conn.execute(f"ALTER TABLE {SNAPSHOT_TABLE} ADD COLUMN discord_name TEXT")
            if "on_leaderboard" not in existing:
                conn.execute(
                    f"ALTER TABLE {SNAPSHOT_TABLE} ADD COLUMN on_leaderboard INTEGER NOT NULL DEFAULT 0"
                )
            conn.commit()
        _SNAPSHOT_COLUMNS = None
    except sqlite3.Error as exc:
        log.error("Failed to ensure snapshot schema: %s", exc)


def _ensure_snapshot_tables() -> None:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            _init_snapshot_db(conn, SNAPSHOT_TABLE)
    except sqlite3.Error as exc:
        log.error("Failed to ensure snapshot tables: %s", exc)


def _ensure_summary_schema() -> None:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SUMMARY_TABLE} (
                    discord_id TEXT PRIMARY KEY,
                    discord_name TEXT,
                    wallets TEXT,
                    total_holdings REAL NOT NULL DEFAULT 0,
                    total_donated_usd REAL NOT NULL DEFAULT 0,
                    leaderboard_visible INTEGER NOT NULL DEFAULT 0,
                    roles TEXT,
                    updated_at TEXT
                )
                """
            )
            existing_cols = {
                row[1] for row in conn.execute(f"PRAGMA table_info({SUMMARY_TABLE})")
            }
            if "roles" not in existing_cols:
                conn.execute(f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN roles TEXT")
            if "anonymous_id" not in existing_cols:
                conn.execute(f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN anonymous_id INTEGER")
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to ensure summary schema: %s", exc)


def _ensure_dm_state_schema() -> None:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dm_state (
                    discord_id TEXT PRIMARY KEY,
                    intro_sent INTEGER NOT NULL DEFAULT 0,
                    intro_sent_at TEXT
                )
                """
            )
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to ensure DM state schema: %s", exc)


def _has_sent_intro_dm(discord_id: str) -> bool:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            row = conn.execute(
                "SELECT intro_sent FROM dm_state WHERE discord_id = ?",
                (discord_id,),
            ).fetchone()
        return bool(row and int(row[0] or 0) == 1)
    except sqlite3.Error:
        return False


def _mark_intro_dm_sent(discord_id: str) -> None:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            conn.execute(
                """
                INSERT INTO dm_state (discord_id, intro_sent, intro_sent_at)
                VALUES (?, 1, datetime('now'))
                ON CONFLICT(discord_id) DO UPDATE SET
                    intro_sent = 1,
                    intro_sent_at = datetime('now')
                """,
                (discord_id,),
            )
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to mark intro DM sent: %s", exc)


def _ensure_live_state_schema() -> None:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS live_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    live_enabled INTEGER NOT NULL DEFAULT 0,
                    enabled_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO live_state (id, live_enabled)
                VALUES (1, 0)
                """
            )
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to ensure live state schema: %s", exc)


def _is_live_enabled() -> bool:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            row = conn.execute(
                "SELECT live_enabled FROM live_state WHERE id = 1"
            ).fetchone()
        return bool(row and int(row[0] or 0) == 1)
    except sqlite3.Error:
        return False


def _set_live_enabled(enabled: bool) -> None:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            conn.execute(
                """
                UPDATE live_state
                SET live_enabled = ?,
                    enabled_at = CASE
                        WHEN ? = 1 THEN datetime('now')
                        ELSE enabled_at
                    END
                WHERE id = 1
                """,
                (1 if enabled else 0, 1 if enabled else 0),
            )
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to set live state: %s", exc)


def _ensure_otp_schema() -> None:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            _ensure_otp_registry(conn)
            _expire_otps(conn)
    except sqlite3.Error as exc:
        log.error("Failed to ensure OTP schema: %s", exc)


 


def _ensure_exchange_wallets_schema() -> None:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS exchange_wallets (
                    wallet_address TEXT PRIMARY KEY,
                    exchange_name TEXT,
                    added_at TEXT,
                    anonymous_id INTEGER
                )
                """
            )
            existing_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(exchange_wallets)")
            }
            if "anonymous_id" not in existing_cols:
                conn.execute("ALTER TABLE exchange_wallets ADD COLUMN anonymous_id INTEGER")
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to ensure exchange wallets schema: %s", exc)


def _ensure_targets_schema() -> None:
    """Create the targets table in STATE_DB for fundraising milestones."""
    try:
        with _connect_db(STATE_DB) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_amount REAL NOT NULL,
                    target_name TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT,
                    completed_at TEXT,
                    order_index INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to ensure targets schema: %s", exc)


def _add_target(amount: float, name: str | None = None) -> int | None:
    """Add a new fundraising target. Returns the new target id."""
    try:
        with _connect_db(STATE_DB) as conn:
            row = conn.execute("SELECT MAX(order_index) FROM targets").fetchone()
            next_order = int(row[0] or 0) + 1 if row and row[0] is not None else 1
            cur = conn.execute(
                """
                INSERT INTO targets (target_amount, target_name, is_active, created_at, order_index)
                VALUES (?, ?, 1, datetime('now'), ?)
                """,
                (amount, name, next_order),
            )
            conn.commit()
            return cur.lastrowid
    except sqlite3.Error as exc:
        log.error("Failed to add target: %s", exc)
        return None


def _remove_target(target_id: int) -> bool:
    """Deactivate a target by id."""
    try:
        with _connect_db(STATE_DB) as conn:
            cur = conn.execute(
                "UPDATE targets SET is_active = 0 WHERE id = ?",
                (target_id,),
            )
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.Error as exc:
        log.error("Failed to remove target: %s", exc)
        return False


def _fetch_targets() -> List[Dict]:
    """Fetch all active targets ordered by order_index."""
    try:
        with _connect_db(STATE_DB) as conn:
            rows = conn.execute(
                """
                SELECT id, target_amount, target_name, is_active, created_at, completed_at, order_index
                FROM targets
                WHERE is_active = 1
                ORDER BY order_index ASC
                """
            ).fetchall()
        return [
            {
                "id": r[0],
                "target_amount": float(r[1]),
                "target_name": r[2],
                "is_active": bool(r[3]),
                "created_at": r[4],
                "completed_at": r[5],
                "order_index": r[6],
            }
            for r in rows
        ]
    except sqlite3.Error as exc:
        log.error("Failed to fetch targets: %s", exc)
        return []


def _update_target_completion(total_raised: float) -> None:
    """Mark targets as completed if total_raised meets them."""
    try:
        with _connect_db(STATE_DB) as conn:
            conn.execute(
                """
                UPDATE targets
                SET completed_at = datetime('now')
                WHERE is_active = 1
                  AND completed_at IS NULL
                  AND target_amount <= ?
                """,
                (total_raised,),
            )
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to update target completion: %s", exc)


def _fetch_next_target(total_raised: float) -> Dict | None:
    """Fetch the next uncompleted target."""
    try:
        with _connect_db(STATE_DB) as conn:
            row = conn.execute(
                """
                SELECT id, target_amount, target_name, created_at, order_index
                FROM targets
                WHERE is_active = 1 AND (completed_at IS NULL OR target_amount > ?)
                ORDER BY order_index ASC
                LIMIT 1
                """,
                (total_raised,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "target_amount": float(row[1]),
            "target_name": row[2],
            "created_at": row[3],
            "order_index": row[4],
        }
    except sqlite3.Error as exc:
        log.error("Failed to fetch next target: %s", exc)
        return None


def _fetch_total_by_token() -> Dict[str, float]:
    """Fetch total raised broken down by token type."""
    result = {"USDC": 0.0, "USDT": 0.0, "FARTBOY": 0.0, "SOL": 0.0}
    try:
        _ensure_tx_table()
        with sqlite3.connect(TX_DB) as conn:
            rows = conn.execute(
                f"""
                SELECT token, SUM(value_usdc)
                FROM {TX_TABLE}
                GROUP BY token
                """
            ).fetchall()
        for token, total in rows:
            if token in result:
                result[token] = float(total or 0)
            else:
                result[token] = float(total or 0)
    except sqlite3.Error as exc:
        log.error("Failed to fetch totals by token: %s", exc)
    return result


def _get_exchange_wallets() -> Set[str]:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            _ensure_exchange_wallets_schema()
            rows = conn.execute(
                "SELECT wallet_address FROM exchange_wallets"
            ).fetchall()
        return {row[0] for row in rows if row and row[0]}
    except sqlite3.Error as exc:
        log.error("Failed to load exchange wallets: %s", exc)
        return set()


def _allocate_otp(discord_id: str, discord_name: str) -> Optional[Tuple[str, int]]:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            _ensure_otp_registry(conn)
            _expire_otps(conn)
            existing = conn.execute(
                f"""
                SELECT otp_value, tick_size
                FROM {OTP_TABLE}
                WHERE assigned_to_discord_id = ?
                  AND status = 'assigned'
                """,
                (discord_id,),
            ).fetchone()
            if existing:
                return existing[0], int(existing[1])

            # Determine tick size based on usage.
            used_count = conn.execute(
                f"SELECT COUNT(1) FROM {OTP_TABLE} WHERE tick_size = 5"
            ).fetchone()[0]
            tick = 6 if used_count >= 95000 else 5

            import secrets

            attempts = 0
            while attempts < 2000:
                attempts += 1
                max_val = 999999 if tick == 6 else 99999
                n = secrets.randbelow(max_val) + 1
                otp_value = f"0.{n:0{tick}d}"
                cur = conn.execute(
                    f"""
                    INSERT OR IGNORE INTO {OTP_TABLE} (
                        otp_value, tick_size, status,
                        assigned_to_discord_id, assigned_to_name, assigned_at
                    )
                    VALUES (?, ?, 'assigned', ?, ?, datetime('now'))
                    """,
                    (otp_value, tick, discord_id, discord_name),
                )
                if cur.rowcount == 1:
                    conn.commit()
                    return otp_value, tick

            # If exhausted at 5-decimal, retry at 6 decimals.
            if tick == 5:
                tick = 6
                attempts = 0
                while attempts < 5000:
                    attempts += 1
                    n = secrets.randbelow(999999) + 1
                    otp_value = f"0.{n:06d}"
                    cur = conn.execute(
                        f"""
                        INSERT OR IGNORE INTO {OTP_TABLE} (
                            otp_value, tick_size, status,
                            assigned_to_discord_id, assigned_to_name, assigned_at
                        )
                        VALUES (?, ?, 'assigned', ?, ?, datetime('now'))
                        """,
                        (otp_value, tick, discord_id, discord_name),
                    )
                    if cur.rowcount == 1:
                        conn.commit()
                        return otp_value, tick
            return None
    except sqlite3.Error as exc:
        log.error("Failed to allocate OTP: %s", exc)
        return None


def _parse_sqlite_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _format_remaining(assigned_at: Optional[str]) -> str:
    assigned_dt = _parse_sqlite_dt(assigned_at)
    if not assigned_dt:
        return f"{OTP_EXPIRY_SECONDS // 60} minutes"
    now = datetime.now(timezone.utc)
    remaining = OTP_EXPIRY_SECONDS - int((now - assigned_dt).total_seconds())
    if remaining <= 0:
        return "0m 0s"
    minutes = remaining // 60
    seconds = remaining % 60
    return f"{minutes}m {seconds}s"


async def _send_first_dm_extras(user: discord.User) -> None:
    _ensure_dm_state_schema()
    discord_id = str(user.id)
    if _has_sent_intro_dm(discord_id):
        return
    war_chest = os.getenv("WARCHEST_ADDRESS", "").strip()
    try:
        await user.send("War chest address:")
        await user.send(war_chest or "War chest address is not configured.")
        await user.send("Quick commands:", view=VerificationButtonsDMView())
        _mark_intro_dm_sent(discord_id)
    except discord.HTTPException:
        log.warning("Failed to send intro DM extras for discord id %s", discord_id)


async def _send_dm_with_intro(user: discord.User, message: str) -> None:
    await user.send(message)
    await _send_first_dm_extras(user)


def _welcome_message_text() -> str:
    return (
        "Welcome! This bot helps you verify your wallet and track donations.\n\n"
        "Start with /verifywallet to get a small verification amount to send to the war chest. "
        "After sending, use /verifystatus to check your verification.\n\n"
        "Other helpful commands: /mywallets, /mytransactions, /leaderboard, "
        "/leaderboardvisibility."
    )


async def _send_full_welcome_dm(user: discord.User) -> bool:
    _ensure_dm_state_schema()
    discord_id = str(user.id)
    war_chest = os.getenv("WARCHEST_ADDRESS", "").strip()
    try:
        await user.send(_welcome_message_text())
        await user.send("War chest address:")
        await user.send(war_chest or "War chest address is not configured.")
        await user.send("Quick commands:", view=VerificationButtonsDMView())
        _mark_intro_dm_sent(discord_id)
        return True
    except discord.HTTPException:
        log.warning("Failed to send full welcome DM for discord id %s", discord_id)
        return False


def _is_leaderboard_visible(discord_id: str) -> bool:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            row = conn.execute(
                f"""
                SELECT leaderboard_visible
                FROM {SUMMARY_TABLE}
                WHERE discord_id = ?
                """,
                (discord_id,),
            ).fetchone()
        return bool(row and row[0])
    except sqlite3.Error:
        return False


def _reminder_text() -> str:
    return (
        "Reminder: you are not on the leaderboard yet. "
        "Use /leaderboardvisibility if you want your name shown. "
        "You can also remove yourself anytime to make your donations anonymous again. "
        "This is voluntary and does not affect any perks."
    )


def _discord_time_from_sqlite(value: Optional[str]) -> str:
    dt = _parse_sqlite_dt(value)
    if not dt:
        return "unknown time"
    return f"<t:{int(dt.timestamp())}:f>"


def _discord_time_from_value(value: Optional[object]) -> str:
    if value is None:
        return "unknown time"
    if isinstance(value, (int, float)):
        return f"<t:{int(value)}:f>"
    if isinstance(value, str):
        dt = _parse_sqlite_dt(value)
        if not dt:
            try:
                normalized = value.replace("Z", "+00:00")
                dt = datetime.fromisoformat(normalized)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except ValueError:
                dt = None
        if dt:
            return f"<t:{int(dt.timestamp())}:f>"
    return "unknown time"



def _init_state_db() -> None:
    with _connect_db(STATE_DB) as conn:
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(leaderboard_state)")
        }
        if existing_cols and "leaderboard_type" not in existing_cols:
            conn.execute("DROP TABLE IF EXISTS leaderboard_state")
            existing_cols = set()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leaderboard_state (
                leaderboard_type TEXT PRIMARY KEY,
                channel_id TEXT,
                message_id TEXT,
                display_limit INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO leaderboard_state (leaderboard_type, display_limit)
            VALUES
                ('donations', ?),
                ('recent', ?)
            """,
            (DEFAULT_LIMIT, DEFAULT_LIMIT),
        )
        conn.commit()


def _save_state(leaderboard_type: str, channel_id: int, message_id: int, display_limit: int) -> None:
    with _connect_db(STATE_DB) as conn:
        conn.execute(
            """
            UPDATE leaderboard_state
            SET channel_id = ?, message_id = ?, display_limit = ?
            WHERE leaderboard_type = ?
            """,
            (str(channel_id), str(message_id), display_limit, leaderboard_type),
        )
        conn.commit()


def _load_state(leaderboard_type: str) -> Tuple[int | None, int | None, int]:
    with _connect_db(STATE_DB) as conn:
        row = conn.execute(
            """
            SELECT channel_id, message_id, display_limit
            FROM leaderboard_state
            WHERE leaderboard_type = ?
            """,
            (leaderboard_type,),
        ).fetchone()
    if not row:
        return None, None, DEFAULT_LIMIT
    channel_id = int(row[0]) if row[0] else None
    message_id = int(row[1]) if row[1] else None
    display_limit = int(row[2]) if row[2] else DEFAULT_LIMIT
    return channel_id, message_id, display_limit


def _set_limit(leaderboard_type: str, display_limit: int) -> None:
    with _connect_db(STATE_DB) as conn:
        conn.execute(
            """
            UPDATE leaderboard_state
            SET display_limit = ?
            WHERE leaderboard_type = ?
            """,
            (display_limit, leaderboard_type),
        )
        conn.commit()


def _format_wallet(addr: str) -> str:
    return addr[:8] if addr else "UNKNOWN"


def _wrap_label(text: str, width: int = 18) -> List[str]:
    if not text:
        return [""]
    text = str(text)
    return [text[i : i + width] for i in range(0, len(text), width)]


def _fetch_donors(limit: Optional[int] = None) -> List[Tuple[str, float, str | None, str | None, int]]:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            summary_rows = conn.execute(
                f"""
                SELECT discord_id, discord_name, total_donated_usd, leaderboard_visible, anonymous_id
                FROM {SUMMARY_TABLE}
                """,
            ).fetchall()
            snapshot_rows = conn.execute(
                f"""
                SELECT wallet_address, donated_usd, discord_id
                FROM {SNAPSHOT_TABLE}
                WHERE donated_usd > 0
                """,
            ).fetchall()
            exchange_rows = conn.execute(
                "SELECT wallet_address FROM exchange_wallets"
            ).fetchall()

        exchange_wallets = {r[0] for r in exchange_rows if r and r[0]}
        combined: List[Tuple[str, float, str | None, str | None, int]] = []

        # Aggregate verified users using summary rows.
        for (
            discord_id,
            discord_name,
            total_donated_usd,
            leaderboard_visible,
            anonymous_id,
        ) in summary_rows:
            if not discord_id:
                continue
            if int(leaderboard_visible or 0) == 1:
                combined.append(
                    ("", float(total_donated_usd or 0), discord_id, discord_name, 1)
                )
            else:
                anon_id = int(anonymous_id or 0) or _get_or_create_anonymous_id(discord_id)
                combined.append(
                    (
                        f"Anonymous donor {anon_id}",
                        float(total_donated_usd or 0),
                        None,
                        None,
                        0,
                    )
                )

        # Add unverified wallets (including verified users with visibility off).
        for wallet, donated_usd, discord_id in snapshot_rows:
            if discord_id:
                continue
            if wallet in exchange_wallets:
                continue
            combined.append((wallet, float(donated_usd or 0), None, None, 0))

        combined.sort(key=lambda r: r[1], reverse=True)
        return combined if limit is None else combined[:limit]
    except sqlite3.Error as exc:
        log.error("Failed to read snapshot DB %s:%s - %s", SNAPSHOT_DB, SNAPSHOT_TABLE, exc)
        return []


def _fetch_top_donors(limit: int = DEFAULT_LIMIT) -> List[Tuple[str, float, str | None, str | None, int]]:
    return _fetch_donors(limit)


def _ensure_tx_table() -> None:
    try:
        with sqlite3.connect(TX_DB) as conn:
            _init_transactions_db(conn, TX_TABLE)
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to ensure transactions table %s:%s - %s", TX_DB, TX_TABLE, exc)


def _fetch_total_donations_usd() -> float:
    try:
        _ensure_tx_table()
        with sqlite3.connect(TX_DB) as conn:
            row = conn.execute(
                f"SELECT SUM(value_usdc) FROM {TX_TABLE}"
            ).fetchone()
        return float(row[0] or 0)
    except sqlite3.Error as exc:
        log.error("Failed to total donations from %s:%s - %s", TX_DB, TX_TABLE, exc)
        return 0.0


def _get_snapshot_columns() -> Set[str]:
    global _SNAPSHOT_COLUMNS
    if _SNAPSHOT_COLUMNS is not None:
        return _SNAPSHOT_COLUMNS
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            rows = conn.execute(f"PRAGMA table_info({SNAPSHOT_TABLE})").fetchall()
        _SNAPSHOT_COLUMNS = {row[1] for row in rows}
    except sqlite3.Error as exc:
        log.error("Failed to inspect snapshot DB %s:%s - %s", SNAPSHOT_DB, SNAPSHOT_TABLE, exc)
        _SNAPSHOT_COLUMNS = set()
    return _SNAPSHOT_COLUMNS


def _find_wallets_for_discord_id(discord_id: str) -> List[str]:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            rows = conn.execute(
                f"""
                SELECT wallet_address
                FROM {SNAPSHOT_TABLE}
                WHERE discord_id = ?
                """,
                (discord_id,),
            ).fetchall()
        return [r[0] for r in rows if r and r[0]]
    except sqlite3.Error as exc:
        log.error("Failed to lookup wallets for discord id: %s", exc)
        return []


def _discord_id_for_wallet(wallet: str) -> str | None:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            row = conn.execute(
                f"""
                SELECT discord_id
                FROM {SNAPSHOT_TABLE}
                WHERE wallet_address = ?
                """,
                (wallet,),
            ).fetchone()
        return row[0] if row and row[0] else None
    except sqlite3.Error:
        return None


def _recompute_summary_for_discord_id(discord_id: str) -> None:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            wallets = conn.execute(
                f"""
                SELECT wallet_address, amount_fartboy, donated_usd, discord_name
                FROM {SNAPSHOT_TABLE}
                WHERE discord_id = ?
                """,
                (discord_id,),
            ).fetchall()
            if not wallets:
                return
            total_holdings = sum(float(r[1] or 0) for r in wallets)
            total_donated_usd = sum(float(r[2] or 0) for r in wallets)
            discord_name = next((r[3] for r in wallets if r[3]), None)
            wallet_list = ",".join(sorted({r[0] for r in wallets if r[0]}))

            existing = conn.execute(
                f"""
                SELECT leaderboard_visible, roles
                FROM {SUMMARY_TABLE}
                WHERE discord_id = ?
                """,
                (discord_id,),
            ).fetchone()
            leaderboard_visible = int(existing[0]) if existing else 0
            roles = existing[1] if existing else None
            conn.execute(
                f"""
                INSERT INTO {SUMMARY_TABLE} (
                    discord_id, discord_name, wallets, total_holdings,
                    total_donated_usd, leaderboard_visible, roles, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(discord_id) DO UPDATE SET
                    discord_name = excluded.discord_name,
                    wallets = excluded.wallets,
                    total_holdings = excluded.total_holdings,
                    total_donated_usd = excluded.total_donated_usd,
                    updated_at = datetime('now')
                """,
                (
                    discord_id,
                    discord_name,
                    wallet_list,
                    total_holdings,
                    total_donated_usd,
                    leaderboard_visible,
                    roles,
                ),
            )
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to recompute summary: %s", exc)


def _set_summary_visibility(discord_id: str, visible: int) -> None:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            conn.execute(
                f"""
                INSERT INTO {SUMMARY_TABLE} (discord_id, leaderboard_visible, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(discord_id) DO UPDATE SET
                    leaderboard_visible = excluded.leaderboard_visible,
                    updated_at = datetime('now')
                """,
                (discord_id, visible),
            )
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to set summary visibility: %s", exc)


def _get_or_create_anonymous_id(discord_id: str) -> int:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            row = conn.execute(
                f"""
                SELECT anonymous_id
                FROM {SUMMARY_TABLE}
                WHERE discord_id = ?
                """,
                (discord_id,),
            ).fetchone()
            if row and row[0]:
                return int(row[0])
            row = conn.execute(
                f"SELECT MAX(anonymous_id) FROM {SUMMARY_TABLE}"
            ).fetchone()
            next_id = int(row[0] or 0) + 1
            conn.execute(
                f"""
                INSERT INTO {SUMMARY_TABLE} (discord_id, anonymous_id, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(discord_id) DO UPDATE SET
                    anonymous_id = excluded.anonymous_id,
                    updated_at = datetime('now')
                """,
                (discord_id, next_id),
            )
            conn.commit()
            return next_id
    except sqlite3.Error as exc:
        log.error("Failed to assign anonymous id: %s", exc)
        return 0


def _fetch_transactions_for_wallet(wallet: str, limit: int) -> List[dict]:
    try:
        _ensure_tx_table()
        with sqlite3.connect(TX_DB) as conn:
            rows = conn.execute(
                f"""
                SELECT timestamp, amount_ui, token, value_usdc, value_fartboy
                FROM {TX_TABLE}
                WHERE sender_wallet = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (wallet, limit),
            ).fetchall()
        results = []
        for ts, amount_ui, token, value_usdc, value_fartboy in rows:
            results.append(
                {
                    "timestamp": ts,
                    "amount_ui": amount_ui,
                    "token": token,
                    "value_usdc": value_usdc,
                    "value_fartboy": value_fartboy,
                }
            )
        return results
    except sqlite3.Error as exc:
        log.error("Failed to read transactions DB: %s", exc)
        return []


def _fetch_transactions_for_discord_id(discord_id: str, limit: int) -> List[dict]:
    try:
        _ensure_tx_table()
        with sqlite3.connect(TX_DB) as conn:
            rows = conn.execute(
                f"""
                SELECT timestamp, amount_ui, token, value_usdc, value_fartboy, sender_wallet
                FROM {TX_TABLE}
                WHERE discord_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (discord_id, limit),
            ).fetchall()
        results = []
        for ts, amount_ui, token, value_usdc, value_fartboy, sender_wallet in rows:
            results.append(
                {
                    "timestamp": ts,
                    "amount_ui": amount_ui,
                    "token": token,
                    "value_usdc": value_usdc,
                    "value_fartboy": value_fartboy,
                    "wallet": sender_wallet,
                }
            )
        return results
    except sqlite3.Error as exc:
        log.error("Failed to read transactions DB by discord_id: %s", exc)
        return []


def _fetch_transactions_for_wallets(wallets: List[str], limit: int) -> List[dict]:
    results: List[dict] = []
    for wallet in wallets:
        rows = _fetch_transactions_for_wallet(wallet, limit)
        for row in rows:
            row["wallet"] = wallet
        results.extend(rows)
    # Sort by timestamp string (ISO) desc; fall back to original order if missing.
    results.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    return results[:limit]

async def _process_signature(signature: str, discord_id: str | None = None) -> Tuple[int, int]:
    api_key = os.getenv("HELIUS_API_KEY")
    fartboy_mint = os.getenv("FARTBOY_MINT")
    target_wallet = os.getenv("WARCHEST_ADDRESS")
    if not api_key or not fartboy_mint or not target_wallet:
        return 0, 0

    rows: List[Dict] = []
    async with HeliusRPCClient(api_key, request_delay=RPC_REQUEST_DELAY) as client:
        tx = await client.get_transaction(signature)
        if not tx:
            return 0, 0

        token_map = _token_account_map(tx)
        for inst in _iter_system_transfer_instructions(tx):
            info = inst.get("parsed", {}).get("info", {})
            source = info.get("source")
            destination = info.get("destination")
            lamports = int(info.get("lamports", 0))
            if not source or not destination or lamports <= 0:
                continue
            if destination != target_wallet:
                continue
            rows.append(
                {
                    "signature": signature,
                    "timestamp": tx.get("blockTime"),
                    "amount_raw": lamports,
                    "amount_ui": _ui_amount(lamports, SOL_DECIMALS),
                    "token": "SOL",
                    "sender_wallet": source,
                }
            )

        allowed_spl_mints = {fartboy_mint, USDC_MINT, USDT_MINT}
        for inst in _iter_token_transfer_instructions(tx):
            parsed = _parse_transfer(inst, token_map)
            if not parsed:
                continue
            source, destination, amount_raw, decimals, mint = parsed
            destination_owner = token_map.get(destination, {}).get("owner")
            source_owner = token_map.get(source, {}).get("owner")
            if not destination_owner or not source_owner:
                continue
            if destination_owner != target_wallet and destination != target_wallet:
                continue
            if mint not in allowed_spl_mints:
                continue
            if mint == fartboy_mint:
                token_label = "FARTBOY"
            elif mint == USDT_MINT:
                token_label = "USDT"
            else:
                token_label = "USDC"
            rows.append(
                {
                    "signature": signature,
                    "timestamp": tx.get("blockTime"),
                    "amount_raw": amount_raw,
                    "amount_ui": _ui_amount(amount_raw, decimals),
                    "decimals": decimals,
                    "token": token_label,
                    "sender_wallet": source_owner,
                }
            )

    if not rows:
        return 0, 0

    computed = await _compute_values_for_rows(rows, fartboy_mint)
    if not computed:
        computed = []

    inserted = 0
    exchange_wallets = _get_exchange_wallets()
    with sqlite3.connect(TX_DB) as tx_conn, sqlite3.connect(SNAPSHOT_DB) as snapshot_conn:
        _init_transactions_db(tx_conn, TX_TABLE)
        _init_snapshot_db(snapshot_conn, SNAPSHOT_TABLE)
        _ensure_otp_registry(snapshot_conn)
        _expire_otps(snapshot_conn)
        for row, value_usdc, value_fartboy in computed:
            row_discord_id = (
                discord_id
                if discord_id and row.get("sender_wallet") not in exchange_wallets
                else None
            )
            cur = tx_conn.cursor()
            cur.execute(
                f"""
                INSERT OR IGNORE INTO {TX_TABLE} (
                    signature, timestamp, sender_wallet, token, amount_raw,
                    amount_ui, value_usdc, value_fartboy, discord_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["signature"],
                    _to_iso(row["timestamp"]) if isinstance(row["timestamp"], int) else row["timestamp"],
                    row["sender_wallet"],
                    row["token"],
                    row["amount_raw"],
                    row["amount_ui"],
                    value_usdc,
                    value_fartboy,
                    row_discord_id,
                ),
            )
            tx_conn.commit()
            if cur.rowcount == 1:
                inserted += 1
                _apply_donations(
                    snapshot_conn,
                    SNAPSHOT_TABLE,
                    row["sender_wallet"],
                    value_fartboy,
                    value_usdc,
                )
            if row_discord_id:
                tx_conn.execute(
                    f"""
                    UPDATE {TX_TABLE}
                    SET discord_id = ?
                    WHERE signature = ?
                      AND sender_wallet = ?
                      AND token = ?
                      AND amount_raw = ?
                    """,
                    (
                        row_discord_id,
                        row["signature"],
                        row["sender_wallet"],
                        row["token"],
                        row["amount_raw"],
                    ),
                )
                tx_conn.commit()
        # Always attempt OTP matching for small FARTBOY transfers, even if the
        # transaction already exists or pricing was unavailable.
        for row in rows:
            if row.get("token") != "FARTBOY" or row.get("amount_ui", 0) >= 1:
                continue
            if row.get("sender_wallet") in exchange_wallets:
                continue
            decimals = int(row.get("decimals", 0))
            amount_raw = int(row.get("amount_raw", 0))
            for tick in (5, 6):
                otp_value = _format_otp_if_exact(amount_raw, decimals, tick)
                if not otp_value:
                    continue
                matched = _match_assigned_otp(
                    snapshot_conn,
                    otp_value,
                    tick,
                    row["signature"],
                    row["sender_wallet"],
                )
                if matched:
                    discord_id, discord_name = matched
                    snapshot_conn.execute(
                        f"""
                        UPDATE {SNAPSHOT_TABLE}
                        SET discord_id = ?, discord_name = ?, on_leaderboard = 0
                        WHERE wallet_address = ?
                          AND (discord_id IS NULL OR discord_id = ?)
                        """,
                        (discord_id, discord_name, row["sender_wallet"], discord_id),
                    )
                    snapshot_conn.commit()
                    _recompute_summary_for_discord_id(discord_id)
                else:
                    _record_used_otp(
                        snapshot_conn,
                        otp_value,
                        tick,
                        row["signature"],
                        row["sender_wallet"],
                    )
    return len(rows), inserted


def _update_wallet_verification(
    wallet: str,
    discord_id: str,
    discord_name: str,
    on_leaderboard: int | None,
) -> bool:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            row = conn.execute(
                f"""
                SELECT wallet_address
                FROM {SNAPSHOT_TABLE}
                WHERE wallet_address = ?
                """,
                (wallet,),
            ).fetchone()
            if not row:
                return False

            cols = _get_snapshot_columns()
            updates: list[str] = []
            params: list = []
            if "discord_id" in cols:
                updates.append("discord_id = ?")
                params.append(discord_id)
            if "discord_name" in cols:
                updates.append("discord_name = ?")
                params.append(discord_name)
            if "on_leaderboard" in cols and on_leaderboard is not None:
                updates.append("on_leaderboard = ?")
                params.append(on_leaderboard)
            if not updates:
                return False
            params.append(wallet)
            conn.execute(
                f"""
                UPDATE {SNAPSHOT_TABLE}
                SET {", ".join(updates)}
                WHERE wallet_address = ?
                """,
                params,
            )
            conn.commit()
        return True
    except sqlite3.Error as exc:
        log.error("Failed to update wallet verification: %s", exc)
        return False


def _lookup_wallet(wallet: str) -> tuple[bool, float]:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            row = conn.execute(
                f"""
                SELECT amount_fartboy
                FROM {SNAPSHOT_TABLE}
                WHERE wallet_address = ?
                """,
                (wallet,),
            ).fetchone()
        if not row:
            return False, 0.0
        return True, float(row[0])
    except sqlite3.Error as exc:
        log.error("Failed to lookup wallet: %s", exc)
        return False, 0.0


def _update_visibility_by_discord(
    discord_id: str,
    discord_name: str,
    on_leaderboard: int,
) -> bool:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            row = conn.execute(
                f"""
                SELECT wallet_address
                FROM {SNAPSHOT_TABLE}
                WHERE discord_id = ?
                """,
                (discord_id,),
            ).fetchone()
            if not row:
                return False

            conn.execute(
                f"""
                UPDATE {SNAPSHOT_TABLE}
                SET on_leaderboard = ?, discord_name = ?
                WHERE discord_id = ?
                """,
                (on_leaderboard, discord_name, discord_id),
            )
            conn.commit()
        return True
    except sqlite3.Error as exc:
        log.error("Failed to update leaderboard visibility: %s", exc)
        return False


def _clear_verification_for_wallets(wallets: List[str]) -> int:
    if not wallets:
        return 0
    affected_discord_ids: Set[str] = set()
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            for wallet in wallets:
                row = conn.execute(
                    f"""
                    SELECT discord_id
                    FROM {SNAPSHOT_TABLE}
                    WHERE wallet_address = ?
                    """,
                    (wallet,),
                ).fetchone()
                if row and row[0]:
                    affected_discord_ids.add(str(row[0]))
                conn.execute(
                    f"""
                    UPDATE {SNAPSHOT_TABLE}
                    SET discord_id = NULL,
                        discord_name = NULL,
                        on_leaderboard = 0
                    WHERE wallet_address = ?
                    """,
                    (wallet,),
                )
            conn.commit()
        with _connect_db(TX_DB) as tx_conn:
            for wallet in wallets:
                tx_conn.execute(
                    f"""
                    UPDATE {TX_TABLE}
                    SET discord_id = NULL
                    WHERE sender_wallet = ?
                    """,
                    (wallet,),
                )
            tx_conn.commit()
        for discord_id in affected_discord_ids:
            with _connect_db(SNAPSHOT_DB) as conn:
                remaining = conn.execute(
                    f"""
                    SELECT 1
                    FROM {SNAPSHOT_TABLE}
                    WHERE discord_id = ?
                    LIMIT 1
                    """,
                    (discord_id,),
                ).fetchone()
                if not remaining:
                    conn.execute(
                        f"DELETE FROM {SUMMARY_TABLE} WHERE discord_id = ?",
                        (discord_id,),
                    )
                    conn.commit()
                    continue
            _recompute_summary_for_discord_id(discord_id)
        return len(wallets)
    except sqlite3.Error as exc:
        log.error("Failed to clear verification wallets: %s", exc)
        return 0


def _render_progress_bar(current: float, target: float, width: int = 20) -> str:
    """Render a text-based progress bar for Discord embeds."""
    if target <= 0:
        return ""
    ratio = min(1.0, max(0.0, current / target))
    filled = round(ratio * width)
    empty = width - filled
    bar = "█" * filled + "░" * empty
    pct = ratio * 100
    return f"`{bar}` {pct:.1f}%"


def _render_target_field(total_raised: float) -> str | None:
    """Build the target/milestone progress string for the embed."""
    _update_target_completion(total_raised)
    next_target = _fetch_next_target(total_raised)
    if not next_target:
        return None
    amount = next_target["target_amount"]
    name = next_target.get("target_name")
    bar = _render_progress_bar(total_raised, amount)
    label = f'"{name}" — ' if name else ""
    return f"{label}${total_raised:,.2f} / ${amount:,.2f}\n{bar}"


def _render_donations_embed(limit: int) -> discord.Embed:
    donors = _fetch_top_donors(limit)
    total_donations = _fetch_total_donations_usd()
    emb = discord.Embed(
        title="Top 100 Donors",
        color=0xFFD166,
        timestamp=datetime.now(timezone.utc),
    )
    emb.add_field(
        name="Total donations",
        value=f"${total_donations:,.2f}",
        inline=False,
    )

    # Progress bar toward next target
    target_text = _render_target_field(total_donations)
    if target_text:
        emb.add_field(
            name="Next target",
            value=target_text,
            inline=False,
        )

    if not donors:
        emb.add_field(name="No data", value="No donations recorded yet.", inline=False)
        emb.set_footer(text="Updated")
        return emb

    lines = []
    for idx, (wallet, donated_usd, discord_id, discord_name, _on_leaderboard) in enumerate(donors, start=1):
        if discord_id:
            display = discord_name or f"<@{discord_id}>"
        elif wallet and (" " in wallet or wallet.startswith("Anonymous donor")):
            display = wallet
        else:
            display = _format_wallet(wallet)
        label_lines = _wrap_label(display, 24)
        if len(label_lines) == 1:
            lines.append(f"{idx:>2}. {label_lines[0]}  —  ${donated_usd:,.2f}")
        else:
            lines.append(f"{idx:>2}. {label_lines[0]}")
            for extra in label_lines[1:]:
                lines.append(f"    {extra}")
            lines.append(f"    ${donated_usd:,.2f}")
    emb.add_field(name="Leaderboard", value="\n".join(lines), inline=False)
    emb.set_footer(text="Updated")
    return emb


def _render_recent_transactions_embed(limit: int = 20) -> discord.Embed:
    emb = discord.Embed(
        title="Recent Incoming Donations",
        color=0x4EA8DE,
        timestamp=datetime.now(timezone.utc),
    )
    try:
        _ensure_tx_table()
        with sqlite3.connect(TX_DB) as conn:
            rows = conn.execute(
                f"""
                SELECT timestamp, amount_ui, token, value_usdc
                FROM {TX_TABLE}
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        rows = []

    if not rows:
        emb.add_field(name="No data", value="No transactions recorded yet.", inline=False)
        emb.set_footer(text="Updated")
        return emb

    lines = []
    for ts, amount_ui, token, value_usdc in rows:
        ts_display = _discord_time_from_value(ts)
        lines.append(f"{ts_display} | ${value_usdc:,.6f} | {amount_ui:g} {token}")
    emb.add_field(
        name="Latest",
        value="Time | Value | Amount\n" + "\n".join(lines),
        inline=False,
    )
    emb.set_footer(text="Updated")
    return emb


class DonationLeaderboardPager(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        donors: List[Tuple[str, float, str | None, str | None, int]],
        page_size: int = 20,
    ) -> None:
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.donors = donors
        self.page_size = max(5, min(page_size, 50))
        self.page = 0
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_button.disabled = self.page <= 0
        total_pages = max(1, (len(self.donors) + self.page_size - 1) // self.page_size)
        self.next_button.disabled = self.page >= (total_pages - 1)

    def _render_page(self) -> str:
        if not self.donors:
            return "No donations recorded yet."
        start = self.page * self.page_size
        end = start + self.page_size
        chunk = self.donors[start:end]
        total_pages = max(1, (len(self.donors) + self.page_size - 1) // self.page_size)
        lines = [f"All donations — page {self.page + 1}/{total_pages}"]
        for idx, (wallet, donated_usd, discord_id, discord_name, _on_leaderboard) in enumerate(
            chunk, start=start + 1
        ):
            if discord_id:
                display = discord_name or f"<@{discord_id}>"
            elif wallet and (" " in wallet or wallet.startswith("Anonymous donor")):
                display = wallet
            else:
                display = _format_wallet(wallet)
            label_lines = _wrap_label(display, 24)
            if len(label_lines) == 1:
                lines.append(f"{idx:>3}. {label_lines[0]}  —  ${donated_usd:,.2f}")
            else:
                lines.append(f"{idx:>3}. {label_lines[0]}")
                for extra in label_lines[1:]:
                    lines.append(f"     {extra}")
                lines.append(f"     ${donated_usd:,.2f}")
        return "\n".join(lines)

    async def _gate(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This leaderboard view is only for the user who opened it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await self._gate(interaction):
            return
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(content=self._render_page(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await self._gate(interaction):
            return
        total_pages = max(1, (len(self.donors) + self.page_size - 1) // self.page_size)
        self.page = min(total_pages - 1, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(content=self._render_page(), view=self)


class VerificationButtonsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify wallet",
        style=discord.ButtonStyle.primary,
        custom_id="verify_wallet_button",
        row=0,
    )
    async def verify_wallet_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "To verify your wallet, run `/verifywallet` here. "
            "This is private and only visible to you.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Verify status",
        style=discord.ButtonStyle.secondary,
        custom_id="verify_status_button",
        row=1,
    )
    async def verify_status_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await verify_status.callback(interaction)

    @discord.ui.button(
        label="My wallets",
        style=discord.ButtonStyle.secondary,
        custom_id="my_wallets_button",
        row=1,
    )
    async def my_wallets_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await my_wallets.callback(interaction)

    @discord.ui.button(
        label="My transactions",
        style=discord.ButtonStyle.secondary,
        custom_id="my_transactions_button",
        row=1,
    )
    async def my_transactions_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await my_transactions.callback(interaction)

    @discord.ui.button(
        label="Leaderboard visibility",
        style=discord.ButtonStyle.secondary,
        custom_id="leaderboard_visibility_button",
        row=2,
    )
    async def leaderboard_visibility_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "To change your leaderboard visibility, run `/leaderboardvisibility` here.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Full leaderboard",
        style=discord.ButtonStyle.secondary,
        custom_id="full_leaderboard_button",
        row=2,
    )
    async def full_leaderboard_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await leaderboard_full.callback(interaction)

    @discord.ui.button(
        label="Open DM",
        style=discord.ButtonStyle.secondary,
        custom_id="open_dm_button",
        row=3,
    )
    async def open_dm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        success = await _send_full_welcome_dm(interaction.user)
        if success:
            await interaction.response.send_message(
                "Check your DMs for the welcome info and buttons.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "I couldn't DM you. Please enable DMs for this server and try again.",
                ephemeral=True,
            )


class VerificationButtonsDMView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify wallet",
        style=discord.ButtonStyle.primary,
        custom_id="verify_wallet_button_dm",
        row=0,
    )
    async def verify_wallet_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "To verify your wallet, run `/verifywallet` here. "
            "This is private and only visible to you.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Verify status",
        style=discord.ButtonStyle.secondary,
        custom_id="verify_status_button_dm",
        row=1,
    )
    async def verify_status_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await verify_status.callback(interaction)

    @discord.ui.button(
        label="My wallets",
        style=discord.ButtonStyle.secondary,
        custom_id="my_wallets_button_dm",
        row=1,
    )
    async def my_wallets_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await my_wallets.callback(interaction)

    @discord.ui.button(
        label="My transactions",
        style=discord.ButtonStyle.secondary,
        custom_id="my_transactions_button_dm",
        row=1,
    )
    async def my_transactions_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await my_transactions.callback(interaction)

    @discord.ui.button(
        label="Leaderboard visibility",
        style=discord.ButtonStyle.secondary,
        custom_id="leaderboard_visibility_button_dm",
        row=2,
    )
    async def leaderboard_visibility_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "To change your leaderboard visibility, run `/leaderboardvisibility` here.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Full leaderboard",
        style=discord.ButtonStyle.secondary,
        custom_id="full_leaderboard_button_dm",
        row=2,
    )
    async def full_leaderboard_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await leaderboard_full.callback(interaction)


class LeaderboardBot(commands.Bot):
    def __init__(self, **kwargs):
        super().__init__(command_prefix="!", help_command=None, **kwargs)
        _init_state_db()
        self._last_snapshot_mtime = 0.0
        self._last_refresh_ts = 0.0
        self._last_render_hash: dict[str, int] = {"donations": 0, "recent": 0}
        self._tracker: IncomingTracker | None = None
        self._tracker_disabled_logged = False
        self._live_enabled = False

    async def setup_hook(self) -> None:
        _ensure_snapshot_tables()
        _ensure_snapshot_schema()
        _ensure_summary_schema()
        _ensure_dm_state_schema()
        _ensure_live_state_schema()
        _ensure_otp_schema()
        _ensure_exchange_wallets_schema()
        _ensure_targets_schema()
        self.add_view(VerificationButtonsView())
        if not self._tracker:
            api_key = os.getenv("HELIUS_API_KEY")
            fartboy_mint = os.getenv("FARTBOY_MINT")
            target_wallet = os.getenv("WARCHEST_ADDRESS")
            if api_key and fartboy_mint and target_wallet:
                self._tracker = IncomingTracker(
                    api_key=api_key,
                    target_wallet=target_wallet,
                    fartboy_mint=fartboy_mint,
                )
            else:
                missing = []
                if not api_key:
                    missing.append("HELIUS_API_KEY")
                if not fartboy_mint:
                    missing.append("FARTBOY_MINT")
                if not target_wallet:
                    missing.append("WARCHEST_ADDRESS")
                log.warning("Tracker disabled. Missing: %s", ", ".join(missing))
        self._live_enabled = _is_live_enabled()
        if self._tracker and self._live_enabled:
            self._tracker.unlimited_backfill = True
        if GUILD_ID:
            if ENABLE_DM_COMMANDS:
                # Avoid duplicate commands in guild by using global commands only.
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
                self.tree.clear_commands(guild=None)
                self.tree.add_command(set_leaderboard_visibility)
                self.tree.add_command(my_transactions)
                self.tree.add_command(my_wallets)
                self.tree.add_command(verify_wallet_otp)
                self.tree.add_command(verify_status)
                self.tree.add_command(leaderboard_full)
                self.tree.add_command(dm_welcome)
                await self.tree.sync()
                log.info("Slash commands synced globally (DMs enabled).")
            else:
                # Clear any global commands to avoid duplicates or stale commands.
                self.tree.clear_commands(guild=None)
                await self.tree.sync()
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.clear_commands(guild=guild)
                self.tree.add_command(set_leaderboard_visibility, guild=guild)
                self.tree.add_command(my_transactions, guild=guild)
                self.tree.add_command(my_wallets, guild=guild)
                self.tree.add_command(verify_wallet_otp, guild=guild)
                self.tree.add_command(verify_status, guild=guild)
                self.tree.add_command(leaderboard_full, guild=guild)
                self.tree.add_command(dm_welcome, guild=guild)
                await self.tree.sync(guild=guild)
                log.info("Slash commands synced to guild %s", GUILD_ID)
        else:
            self.tree.add_command(set_leaderboard_visibility)
            self.tree.add_command(my_transactions)
            self.tree.add_command(my_wallets)
            self.tree.add_command(verify_wallet_otp)
            self.tree.add_command(verify_status)
            self.tree.add_command(leaderboard_full)
            self.tree.add_command(dm_welcome)
            await self.tree.sync()
        self.update_leaderboard.start()
        self.watch_snapshot_changes.start()
        self.track_incoming.start()

    @tasks.loop(seconds=UPDATE_SECONDS)
    async def update_leaderboard(self) -> None:
        await self._refresh_leaderboards()

    async def _refresh_leaderboards(self) -> None:
        now_ts = time.time()
        if now_ts - self._last_refresh_ts < LEADERBOARD_REFRESH_COOLDOWN:
            return
        self._last_refresh_ts = now_ts
        for leaderboard_type in ("donations", "recent"):
            channel_id, message_id, display_limit = _load_state(leaderboard_type)
            if not channel_id or not message_id:
                continue
            channel = self.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                msg = await channel.fetch_message(message_id)
            except discord.NotFound:
                continue
            try:
                if leaderboard_type == "donations":
                    embed = _render_donations_embed(display_limit)
                else:
                    embed = _render_recent_transactions_embed(20)
                embed_dict = embed.to_dict()
                embed_hash = hash(str(embed_dict))
                if self._last_render_hash.get(leaderboard_type) == embed_hash:
                    continue
                await msg.edit(embed=embed, content=None)
                self._last_render_hash[leaderboard_type] = embed_hash
            except discord.HTTPException as exc:
                log.warning("Failed to edit leaderboard message: %s", exc)

    @tasks.loop(seconds=SNAPSHOT_WATCH_SECONDS)
    async def watch_snapshot_changes(self) -> None:
        try:
            mtime = os.path.getmtime(SNAPSHOT_DB)
        except OSError:
            return
        if mtime > self._last_snapshot_mtime:
            self._last_snapshot_mtime = mtime
            await self._refresh_leaderboards()

    @tasks.loop(seconds=TRACKER_INTERVAL_SECONDS)
    async def track_incoming(self) -> None:
        if not self._live_enabled:
            return
        if not self._tracker:
            if not self._tracker_disabled_logged:
                log.warning(
                    "Tracker loop skipped (no tracker configured). Check env vars."
                )
                self._tracker_disabled_logged = True
            return
        try:
            count, wallets, verified_ids = await self._tracker.run_once()
            discord_ids = {did for w in wallets if (did := _discord_id_for_wallet(w))}
            for did in discord_ids:
                _recompute_summary_for_discord_id(did)
            if verified_ids:
                for did in set(verified_ids):
                    try:
                        user = await self.fetch_user(int(did))
                        if user:
                            dm_message = (
                                "Your wallet verification transfer was received. "
                                "Your wallet is now linked."
                            )
                            if not _is_leaderboard_visible(str(did)):
                                dm_message += (
                                    "\n\n"
                                    "Reminder: you are not on the leaderboard yet. "
                                    "Use /leaderboardvisibility if you want your name shown. "
                                    "You can also remove yourself anytime to make your donations anonymous again. "
                                    "This is voluntary and does not affect any perks."
                                )
                            await _send_dm_with_intro(user, dm_message)
                    except discord.HTTPException:
                        log.warning("Failed to DM verification for discord id %s", did)
            log.info("Tracker tick complete. New transactions: %s", count)
        except Exception as exc:
            log.exception("Tracker tick failed: %s", exc)

    @update_leaderboard.before_loop
    async def _before_update_loop(self) -> None:
        await self.wait_until_ready()

    @watch_snapshot_changes.before_loop
    async def _before_watch_loop(self) -> None:
        await self.wait_until_ready()

    @track_incoming.before_loop
    async def _before_track_loop(self) -> None:
        await self.wait_until_ready()

    async def on_ready(self) -> None:
        log.info("Leaderboard bot ready as %s", self.user)

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        # Silently ignore commands sent outside the designated command channel.
        if isinstance(error, commands.CheckFailure) and getattr(error, "_channel_restricted", False):
            return
        await super().on_command_error(ctx, error)


class _ChannelRestricted(commands.CheckFailure):
    """Raised when a ! command is used outside the designated command channel."""
    _channel_restricted = True


def _command_channel_check(ctx: commands.Context) -> bool:
    """Global check: if COMMAND_CHANNEL_ID is set, only allow ! commands in that channel."""
    if not COMMAND_CHANNEL_ID:
        return True  # No restriction configured.
    if ctx.channel and str(ctx.channel.id) == COMMAND_CHANNEL_ID:
        return True
    raise _ChannelRestricted()


intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True

bot = LeaderboardBot(intents=intents)
bot.add_check(_command_channel_check)


@app_commands.command(
    name="leaderboardvisibility",
    description="Update whether your verified wallet shows your name on leaderboards.",
)
@app_commands.describe(on_leaderboard="Show your name on leaderboards")
async def set_leaderboard_visibility(
    interaction: discord.Interaction,
    on_leaderboard: bool,
):
    success = _update_visibility_by_discord(
        discord_id=str(interaction.user.id),
        discord_name=str(interaction.user),
        on_leaderboard=1 if on_leaderboard else 0,
    )
    if not success:
        return await interaction.response.send_message(
            "No verified wallet found for your Discord user. Run /verifywallet first.",
            ephemeral=True,
        )
    await interaction.response.send_message(
        "Updated leaderboard visibility.",
        ephemeral=True,
    )
    _set_summary_visibility(str(interaction.user.id), 1 if on_leaderboard else 0)
    _recompute_summary_for_discord_id(str(interaction.user.id))
    await bot._refresh_leaderboards()


@bot.command(name="checkwallet")
@commands.has_permissions(manage_guild=True)
async def checkwallet(ctx: commands.Context, wallet: str = ""):
    if not wallet:
        return await ctx.send("Usage: `!checkwallet WALLET_ADDRESS`")
    exists, amount = _lookup_wallet(wallet.strip())
    if not exists:
        return await ctx.send("Wallet not found in the current snapshot.")
    await ctx.send(f"Wallet found. Snapshot balance: {amount:,.4f} FARTBOY.")


@app_commands.command(
    name="mytransactions",
    description="Show recent transactions for the wallet linked to your Discord user.",
)
async def my_transactions(interaction: discord.Interaction):
    wallets = _find_wallets_for_discord_id(str(interaction.user.id))
    rows = _fetch_transactions_for_wallets(wallets, TX_LOOKUP_LIMIT)
    if not rows:
        rows = _fetch_transactions_for_discord_id(
            str(interaction.user.id), TX_LOOKUP_LIMIT
        )
    if not rows:
        return await interaction.response.send_message(
            "No transactions found for your wallet.",
            ephemeral=True,
        )
    total_usd = 0.0
    total_holdings = 0.0
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            for wallet in wallets:
                row = conn.execute(
                    f"""
                    SELECT amount_fartboy
                    FROM {SNAPSHOT_TABLE}
                    WHERE wallet_address = ?
                    """,
                    (wallet,),
                ).fetchone()
                if row:
                    total_holdings += float(row[0] or 0)
    except sqlite3.Error:
        total_holdings = 0.0
    lines = []
    for row in rows[:50]:
        lines.append(
            f"{row['timestamp']} | {row['amount_ui']} {row['token']} | "
            f"${row['value_usdc']:.6f}"
        )
        total_usd += float(row["value_usdc"] or 0)
    body = "\n".join(lines)
    total_pct = 0.0
    if total_holdings > 0:
        total_pct = (total_usd / total_holdings) * 100.0
    await interaction.response.send_message(
        f"Transactions from your linked wallet(s):\n```\n{body}\n```\n"
        f"Total donated: ${total_usd:,.6f} ({total_pct:.4f}% of holdings)",
        ephemeral=True,
    )


@app_commands.command(
    name="mywallets",
    description="Show your verified wallets and total donated USD for each wallet.",
)
async def my_wallets(interaction: discord.Interaction):
    wallets = _find_wallets_for_discord_id(str(interaction.user.id))
    if not wallets:
        return await interaction.response.send_message(
            "No verified wallet found. Run /verifywallet first.",
            ephemeral=True,
        )
    lines = []
    total_usd = 0.0
    total_holdings = 0.0
    for wallet in wallets:
        try:
            with _connect_db(SNAPSHOT_DB) as conn:
                row = conn.execute(
                    f"""
                    SELECT donated_usd, amount_fartboy
                    FROM {SNAPSHOT_TABLE}
                    WHERE wallet_address = ?
                    """,
                    (wallet,),
                ).fetchone()
            donated_usd = float(row[0] or 0) if row else 0.0
            holdings = float(row[1] or 0) if row else 0.0
        except sqlite3.Error:
            donated_usd = 0.0
            holdings = 0.0
        total_usd += donated_usd
        total_holdings += holdings

        pct = 0.0
        if holdings > 0:
            pct = (donated_usd / holdings) * 100.0
        lines.append(f"{wallet} | ${donated_usd:,.6f} | {pct:.4f}%")

    total_pct = 0.0
    if total_holdings > 0:
        total_pct = (total_usd / total_holdings) * 100.0
    lines.append(f"Total | ${total_usd:,.6f} | {total_pct:.4f}%")
    body = "\n".join(lines)
    await interaction.response.send_message(
        f"Your verified wallets and donations:\n```\n{body}\n```",
        ephemeral=True,
    )


@app_commands.command(
    name="verifywallet",
    description="Generate a unique small-amount OTP for wallet verification.",
)
async def verify_wallet_otp(interaction: discord.Interaction):
    otp = _allocate_otp(str(interaction.user.id), str(interaction.user))
    if not otp:
        return await interaction.response.send_message(
            "Failed to allocate a verification amount. Please try again later.",
            ephemeral=True,
        )
    otp_value, tick = otp
    war_chest = os.getenv("WARCHEST_ADDRESS", "")
    show_reminder = not _is_leaderboard_visible(str(interaction.user.id))
    assigned_at = None
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            row = conn.execute(
                f"""
                SELECT assigned_at
                FROM {OTP_TABLE}
                WHERE assigned_to_discord_id = ?
                  AND otp_value = ?
                  AND tick_size = ?
                  AND status = 'assigned'
                """,
                (str(interaction.user.id), otp_value, tick),
            ).fetchone()
            assigned_at = row[0] if row else None
    except sqlite3.Error:
        assigned_at = None
    remaining = _format_remaining(assigned_at)
    instructions = (
        "To verify your wallet, send the exact amount of FARTBOY given below to the war chest. "
        "This amount acts as a One-Time-Password (OTP), and will expire after 60 minutes. "
        "You can always run /verifywallet again after that to get a new OTP.\n\n"
        "Your verification should be done within 3 minutes after the OTP amount of FARTBOY has been sent. "
        "You can check the status and trigger immediate verification by running /verifystatus.\n\n"
        "After the verification is complete, you will receive a DM confirming that your wallet is verified. "
        "If you turned DMs off, you can only check the status with /verifystatus."
    )
    if show_reminder:
        instructions += (
            "\n\nAfter the verification is complete, you can run /leaderboardvisibility to put your username with the "
            "amount you donated on the leaderboard, but this is up to you. Leaderboard visibility won't affect any of "
            "your perks, and you can remove yourself any time to make your donations anonymous again."
        )
    await interaction.response.send_message(instructions, ephemeral=True)
    await interaction.followup.send("War chest address:", ephemeral=True)
    await interaction.followup.send(f"{war_chest}", ephemeral=True)
    await interaction.followup.send("Amount:", ephemeral=True)
    await interaction.followup.send(f"{otp_value}", ephemeral=True)


@app_commands.command(
    name="verifystatus",
    description="Show your verification status (pending, used, expired).",
)
async def verify_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if bot._tracker:
        try:
            count, wallets, verified_ids = await bot._tracker.run_once()
            discord_ids = {
                did for w in wallets if (did := _discord_id_for_wallet(w))
            }
            for did in discord_ids:
                _recompute_summary_for_discord_id(did)
            if verified_ids:
                for did in set(verified_ids):
                    try:
                        user = await bot.fetch_user(int(did))
                        if user:
                            dm_message = (
                                "Your wallet verification transfer was received. "
                                "Your wallet is now linked."
                            )
                            if not _is_leaderboard_visible(str(did)):
                                dm_message += (
                                    "\n\n"
                                    "Reminder: you are not on the leaderboard yet. "
                                    "Use /leaderboardvisibility if you want your name shown. "
                                    "You can also remove yourself anytime to make your donations anonymous again. "
                                    "This is voluntary and does not affect any perks."
                                )
                            await _send_dm_with_intro(user, dm_message)
                    except discord.HTTPException:
                        log.warning(
                            "Failed to DM verification for discord id %s", did
                        )
            if count:
                log.info("On-demand tracker run found %s new transactions.", count)
        except Exception as exc:
            log.warning("On-demand tracker run failed: %s", exc)
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            _ensure_otp_registry(conn)
            _expire_otps(conn)
            pending = conn.execute(
                f"""
                SELECT otp_value, tick_size, assigned_at
                FROM {OTP_TABLE}
                WHERE assigned_to_discord_id = ?
                  AND status = 'assigned'
                ORDER BY assigned_at DESC
                LIMIT 1
                """,
                (str(interaction.user.id),),
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT otp_value, tick_size, status, assigned_at, used_at, tx_signature
                FROM {OTP_TABLE}
                WHERE assigned_to_discord_id = ?
                ORDER BY assigned_at DESC
                """,
                (str(interaction.user.id),),
            ).fetchall()
    except sqlite3.Error:
        rows = []

    if not rows:
        message = "No verification records found. Use /verifywallet to start."
        if not _is_leaderboard_visible(str(interaction.user.id)):
            message += "\n\n" + _reminder_text()
        return await interaction.followup.send(message, ephemeral=True)

    war_chest = os.getenv("WARCHEST_ADDRESS", "")
    lines = []
    if pending:
        otp_value, tick_size, assigned_at = pending
        remaining = _format_remaining(assigned_at)
        await interaction.followup.send(
            "Your active verification amount:\n"
            f"```\n{otp_value}\n```\n"
            "Send to:\n"
            f"```\n{war_chest}\n```\n"
            f"Expires in {remaining} (tick size {tick_size} decimals).",
            ephemeral=True,
        )

    for otp_value, tick_size, status, assigned_at, used_at, tx_signature in rows:
        status_text = status or "unknown"
        ts = _discord_time_from_sqlite(used_at or assigned_at)
        sig = f"tx {tx_signature[:12]}…" if tx_signature else "tx none"
        lines.append(
            f"- {otp_value} ({tick_size} decimals) | {status_text} | {ts} | {sig}"
        )

    body = "\n".join(lines[:50])
    await interaction.followup.send(
        f"Your verification history:\n{body}",
        ephemeral=True,
    )
    if not _is_leaderboard_visible(str(interaction.user.id)):
        await interaction.followup.send(_reminder_text(), ephemeral=True)


@app_commands.command(
    name="leaderboard",
    description="Show the full donation leaderboard (ephemeral pagination).",
)
async def leaderboard_full(interaction: discord.Interaction):
    donors = _fetch_donors(limit=None)
    view = DonationLeaderboardPager(interaction.user.id, donors)
    await interaction.response.send_message(
        view._render_page(),
        ephemeral=True,
        view=view,
    )


@app_commands.command(
    name="dm",
    description="Send the welcome info and buttons in a DM.",
)
async def dm_welcome(interaction: discord.Interaction):
    success = await _send_full_welcome_dm(interaction.user)
    if success:
        await interaction.response.send_message(
            "Check your DMs for the welcome info and buttons.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "I couldn't DM you. Please enable DMs for this server and try again.",
            ephemeral=True,
        )


@bot.command(name="setdonationleaderboard")
@commands.has_permissions(manage_guild=True)
async def setdonationleaderboard(
    ctx: commands.Context, channel: discord.TextChannel | None = None, limit: int = DEFAULT_LIMIT
):
    if channel is None:
        return await ctx.send("Usage: `!setdonationleaderboard #channel`")

    limit = max(1, min(limit, 100))
    msg = await channel.send(embed=_render_donations_embed(limit))
    _save_state("donations", channel.id, msg.id, limit)
    await ctx.send(f"Donation leaderboard message set in {channel.mention}.")
    recent_msg = await channel.send(embed=_render_recent_transactions_embed(20))
    _save_state("recent", channel.id, recent_msg.id, 20)
    war_chest = os.getenv("WARCHEST_ADDRESS", "")
    await channel.send(
        "Send FARTBOY, SOL or USDC to the following address to donate:"
    )
    await channel.send(war_chest)
    info = (
        "Wallets can be verified at any time, even after donations. "
        "Donations sent from exchanges are not eligible for perks or leaderboard visibility. "
        "Choosing to appear on the leaderboard is optional and does not affect perks."
    )
    await channel.send(info, view=VerificationButtonsView())


@bot.command(name="setdonationleadersize")
@commands.has_permissions(manage_guild=True)
async def setdonationleadersize(ctx: commands.Context, limit: int = DEFAULT_LIMIT):
    limit = max(1, min(limit, 100))
    _set_limit("donations", limit)
    await ctx.send(f"Donation leaderboard size set to {limit}.")


@bot.command(name="settrackerinterval")
@commands.has_permissions(manage_guild=True)
async def settrackerinterval(ctx: commands.Context, seconds: int = 0):
    if seconds <= 0:
        return await ctx.send("Usage: `!settrackerinterval SECONDS`")
    seconds = max(5, min(seconds, 3600))
    bot.track_incoming.change_interval(seconds=seconds)
    await ctx.send(f"Tracker interval set to {seconds} seconds.")


@bot.command(name="setpagelimit")
@commands.has_permissions(manage_guild=True)
async def setpagelimit(ctx: commands.Context, limit: int = 0):
    if limit <= 0:
        return await ctx.send("Usage: `!setpagelimit LIMIT`")
    limit = max(1, min(limit, 1000))
    if not bot._tracker:
        return await ctx.send("Tracker is not configured (missing env vars).")
    bot._tracker.page_limit = limit
    await ctx.send(f"Tracker page limit set to {limit} signatures per address.")


@bot.command(name="golive")
@commands.has_permissions(manage_guild=True)
async def golive(ctx: commands.Context):
    if not bot._tracker:
        return await ctx.send("Tracker is not configured (missing env vars).")
    if _is_live_enabled():
        return await ctx.send("Already live. Tracking is enabled.")
    await ctx.send("Setting checkpoint to current on-chain state...")
    try:
        address_count = await bot._tracker.init_checkpoint()
    except Exception as exc:
        log.exception("Failed to set go-live checkpoint: %s", exc)
        return await ctx.send("Failed to set checkpoint. Check logs.")
    _set_live_enabled(True)
    bot._live_enabled = True
    bot._tracker.unlimited_backfill = True
    await ctx.send(
        f"Checkpoint set for {address_count} address(es). Tracking is live."
    )


_COMMAND_HELP: Dict[str, Dict[str, str]] = {
    "help": {
        "summary": "Show help for bot commands.",
        "usage": "`!help [command]`",
        "details": "Example: `!help tx`.",
    },
    "checkwallet": {
        "summary": "Check a wallet against the snapshot balances.",
        "usage": "`!checkwallet WALLET_ADDRESS`",
        "details": "Returns the snapshot balance for the wallet if found.",
    },
    "setdonationleaderboard": {
        "summary": "Post the donation and recent transaction leaderboards.",
        "usage": "`!setdonationleaderboard #channel [limit]`",
        "details": "Also posts the war chest address and command buttons.",
    },
    "setdonationleadersize": {
        "summary": "Update the donation leaderboard size.",
        "usage": "`!setdonationleadersize [limit]`",
        "details": "Limit is clamped between 1 and 100.",
    },
    "settrackerinterval": {
        "summary": "Update the Helius polling interval (seconds).",
        "usage": "`!settrackerinterval SECONDS`",
        "details": "Lower values poll more often; higher values reduce API usage.",
    },
    "setpagelimit": {
        "summary": "Update signatures per address page in Helius polling.",
        "usage": "`!setpagelimit LIMIT`",
        "details": "Higher values scan more signatures per run.",
    },
    "golive": {
        "summary": "Set a fresh checkpoint so old transactions are ignored.",
        "usage": "`!golive`",
        "details": "Enables tracking and starts from the latest signatures.",
    },
    "snapshotholders": {
        "summary": "Run the holder snapshot script.",
        "usage": "`!snapshotholders`",
        "details": "Run after a fresh reset to populate balances before go-live.",
    },
    "resetverification": {
        "summary": "Clear all verification data.",
        "usage": "`!resetverification`",
        "details": "Use only if you need to wipe verification state before go-live.",
    },
    "setexchangewallets": {
        "summary": "Mark wallets as exchange wallets.",
        "usage": "`!setexchangewallets EXCHANGE_NAME WALLET1 WALLET2 ...`",
        "details": "Exchange wallets are excluded from perks and verification.",
    },
    "removeexchangewallets": {
        "summary": "Remove wallets from the exchange list.",
        "usage": "`!removeexchangewallets WALLET1 WALLET2 ...`",
        "details": "Does not restore any previous verification links.",
    },
    "manualverify": {
        "summary": "Manually link a wallet to a Discord user.",
        "usage": "`!manualverify @user WALLET_ADDRESS`",
        "details": "Leaderboard visibility stays off until the user opts in.",
    },
    "removeverification": {
        "summary": "Remove verification for a user or wallet.",
        "usage": "`!removeverification WALLET_ADDRESS`",
        "details": "Dangerous: unlinks that wallet and clears any donation links.",
    },
    "synccommands": {
        "summary": "Sync slash commands.",
        "usage": "`!synccommands`",
        "details": "Useful after enabling DM commands or adding new ones.",
    },
    "listcommands": {
        "summary": "List registered slash commands.",
        "usage": "`!listcommands`",
        "details": "Shows both global and guild commands when available.",
    },
    "tx": {
        "summary": "List recent transactions for a user or wallet.",
        "usage": "`!tx @user` or `!tx WALLET_ADDRESS`",
        "details": "Shows up to the configured lookup limit.",
    },
    "addtransaction": {
        "summary": "Manually record a transaction signature.",
        "usage": "`!addtransaction SIGNATURE [@user]`",
        "details": "Optional @user links the donation to that user (including exchange sends).",
    },
    "donationbothelp": {
        "summary": "Legacy help command.",
        "usage": "`!donationbothelp`",
        "details": "Use `!help` for detailed command help.",
    },
    "settarget": {
        "summary": "Add a new fundraising target/milestone.",
        "usage": "`!settarget <amount> [name]`",
        "details": "Amount is in USD. Name is optional (e.g. `!settarget 5000 Marketing Fund`).",
    },
    "removetarget": {
        "summary": "Remove a fundraising target by ID.",
        "usage": "`!removetarget <id>`",
        "details": "Use `!targets` to see target IDs.",
    },
    "targets": {
        "summary": "Show all active targets and progress.",
        "usage": "`!targets`",
        "details": "Displays each target with current progress in USD and percentage.",
    },
}


def _render_help_text(command_name: Optional[str]) -> str:
    if not command_name:
        return ""
    normalized = command_name.lower().lstrip("!")
    info = _COMMAND_HELP.get(normalized)
    if not info:
        return (
            f"Unknown command `{command_name}`.\n"
            "Use `!help` to list available commands."
        )
    return (
        f"!{normalized}\n"
        f"{info['summary']}\n\n"
        f"Usage: {info['usage']}\n"
        f"{info['details']}"
    )


def _build_help_embed(command_name: Optional[str]) -> discord.Embed:
    if command_name:
        normalized = command_name.lower().lstrip("!")
        info = _COMMAND_HELP.get(normalized)
        if not info:
            return discord.Embed(
                title="Unknown command",
                description=(
                    f"Unknown command `{command_name}`.\n"
                    "Use `!help` to list available commands."
                ),
                color=0x4EA8DE,
            )
        emb = discord.Embed(
            title=f"!{normalized}",
            description=info["summary"],
            color=0x4EA8DE,
        )
        emb.add_field(name="Usage", value=info["usage"], inline=False)
        emb.add_field(name="Details", value=info["details"], inline=False)
        return emb

    categories = {
        "Go-live": [
            "snapshotholders",
            "setdonationleaderboard",
            "golive",
        ],
        "Verification admin": [
            "manualverify",
            "removeverification",
            "resetverification",
            "setexchangewallets",
            "removeexchangewallets",
        ],
        "Transactions": [
            "tx",
            "addtransaction",
            "checkwallet",
        ],
        "Targets": [
            "settarget",
            "removetarget",
            "targets",
        ],
        "Admin utilities": [
            "settrackerinterval",
            "setpagelimit",
            "setdonationleadersize",
            "synccommands",
            "listcommands",
            "help",
            "donationbothelp",
        ],
    }
    emb = discord.Embed(
        title="Donation Bot Commands",
        description="Use `!help COMMAND` for details.",
        color=0x4EA8DE,
    )
    for title, keys in categories.items():
        lines = []
        for key in keys:
            info = _COMMAND_HELP.get(key)
            if info:
                lines.append(f"`!{key}` — {info['summary']}")
        if lines:
            emb.add_field(name=title, value="\n".join(lines), inline=False)
    emb.add_field(
        name="Go-live flow",
        value=(
            "1) `!snapshotholders`\n"
            "2) `!setdonationleaderboard #channel [limit]`\n"
            "3) `!golive`"
        ),
        inline=False,
    )
    emb.set_footer(text="Tracking is disabled until you run !golive.")
    return emb


@bot.command(name="help")
async def help_command(ctx: commands.Context, command_name: str = ""):
    await ctx.send(embed=_build_help_embed(command_name or None))


@bot.command(name="donationbothelp")
async def donationbothelp(ctx: commands.Context):
    await ctx.send("Use `!help` for detailed command help.")


@bot.command(name="snapshotholders")
@commands.has_permissions(manage_guild=True)
async def snapshotholders(ctx: commands.Context):
    await ctx.send("Starting snapshot... this can take a few minutes.")
    try:
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "snapshot_fartboy_holders.py"],
            capture_output=True,
            text=True,
            check=True,
        )
        if result.stdout:
            log.info("Snapshot output: %s", result.stdout.strip())
        if result.stderr:
            log.warning("Snapshot stderr: %s", result.stderr.strip())
        await ctx.send("Snapshot completed.")
    except subprocess.CalledProcessError as exc:
        log.exception("Snapshot failed: %s", exc)
        stderr = (exc.stderr or "").strip()
        if stderr:
            snippet = stderr[-1800:]
            await ctx.send(f"Snapshot failed:\n```{snippet}```")
        else:
            await ctx.send("Snapshot failed. Check logs.")
    except Exception as exc:
        log.exception("Snapshot failed: %s", exc)
        await ctx.send("Snapshot failed. Check logs.")


@bot.command(name="resetverification")
@commands.has_permissions(manage_guild=True)
async def resetverification(ctx: commands.Context):
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            conn.execute(
                f"""
                UPDATE {SNAPSHOT_TABLE}
                SET discord_id = NULL,
                    discord_name = NULL,
                    on_leaderboard = 0
                """
            )
            conn.execute(f"DELETE FROM {SUMMARY_TABLE}")
            conn.execute(f"DELETE FROM {OTP_TABLE}")
            conn.commit()
        await ctx.send("Verification data reset.")
        await bot._refresh_leaderboards()
    except sqlite3.Error as exc:
        log.exception("Failed to reset verification: %s", exc)
        await ctx.send("Failed to reset verification. Check logs.")


@bot.command(name="manualverify")
@commands.has_permissions(manage_guild=True)
async def manualverify(
    ctx: commands.Context,
    member: discord.Member | None = None,
    wallet: str = "",
):
    if not member or not wallet:
        return await ctx.send(
            "Usage: `!manualverify @user WALLET_ADDRESS`"
        )
    wallet = wallet.strip()
    if not wallet:
        return await ctx.send("Wallet address is required.")
    if wallet in _get_exchange_wallets():
        return await ctx.send(
            "That wallet is marked as an exchange wallet and cannot be verified."
        )
    on_leaderboard = 0
    success = _update_wallet_verification(
        wallet=wallet,
        discord_id=str(member.id),
        discord_name=str(member),
        on_leaderboard=on_leaderboard,
    )
    if not success:
        return await ctx.send(
            "Wallet not found in the snapshot. Run a snapshot first."
        )
    _set_summary_visibility(str(member.id), on_leaderboard)
    _recompute_summary_for_discord_id(str(member.id))
    try:
        with _connect_db(TX_DB) as tx_conn:
            tx_conn.execute(
                f"""
                UPDATE {TX_TABLE}
                SET discord_id = ?
                WHERE sender_wallet = ?
                """,
                (str(member.id), wallet),
            )
            tx_conn.commit()
    except sqlite3.Error as exc:
        log.warning("Failed to update tx discord id for manual verify: %s", exc)
    await ctx.send(
        f"Linked `{wallet}` to {member.mention} (leaderboard visibility: off)."
    )


@bot.command(name="removeverification")
@commands.has_permissions(manage_guild=True)
async def removeverification(ctx: commands.Context, target: str = ""):
    if not target:
        return await ctx.send(
            "Usage: `!removeverification WALLET_ADDRESS`"
        )
    wallets = [target.strip()]
    removed = _clear_verification_for_wallets(wallets)
    if removed == 0:
        return await ctx.send("No matching wallets were updated.")
    await ctx.send(f"Removed verification for {removed} wallet(s).")


@bot.command(name="setexchangewallets")
@commands.has_permissions(manage_guild=True)
async def setexchangewallets(ctx: commands.Context, exchange_name: str = "", *wallets: str):
    if not exchange_name or not wallets:
        return await ctx.send(
            "Usage: `!setexchangewallets EXCHANGE_NAME WALLET1 WALLET2 ...`"
        )
    cleaned = [w.strip() for w in wallets if w.strip()]
    if not cleaned:
        return await ctx.send("No valid wallet addresses provided.")
    affected_discord_ids: Set[str] = set()
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            _ensure_exchange_wallets_schema()
            row = conn.execute(
                "SELECT MAX(anonymous_id) FROM exchange_wallets"
            ).fetchone()
            next_id = int(row[0] or 0) + 1
            for wallet in cleaned:
                existing = conn.execute(
                    "SELECT anonymous_id FROM exchange_wallets WHERE wallet_address = ?",
                    (wallet,),
                ).fetchone()
                anon_id = existing[0] if existing and existing[0] else next_id
                if not existing or not existing[0]:
                    next_id += 1
                conn.execute(
                    """
                    INSERT INTO exchange_wallets (wallet_address, exchange_name, added_at, anonymous_id)
                    VALUES (?, ?, datetime('now'), ?)
                    ON CONFLICT(wallet_address) DO UPDATE SET
                        exchange_name = excluded.exchange_name,
                        added_at = datetime('now'),
                        anonymous_id = excluded.anonymous_id
                    """,
                    (wallet, exchange_name, anon_id),
                )
                row = conn.execute(
                    f"SELECT discord_id FROM {SNAPSHOT_TABLE} WHERE wallet_address = ?",
                    (wallet,),
                ).fetchone()
                if row and row[0]:
                    affected_discord_ids.add(str(row[0]))
                conn.execute(
                    f"""
                    UPDATE {SNAPSHOT_TABLE}
                    SET discord_id = NULL,
                        discord_name = NULL,
                        on_leaderboard = 0
                    WHERE wallet_address = ?
                    """,
                    (wallet,),
                )
            conn.commit()
        with _connect_db(TX_DB) as tx_conn:
            for wallet in cleaned:
                tx_conn.execute(
                    f"""
                    UPDATE {TX_TABLE}
                    SET discord_id = NULL
                    WHERE sender_wallet = ?
                    """,
                    (wallet,),
                )
            tx_conn.commit()
        for discord_id in affected_discord_ids:
            with _connect_db(SNAPSHOT_DB) as conn:
                remaining = conn.execute(
                    f"""
                    SELECT 1
                    FROM {SNAPSHOT_TABLE}
                    WHERE discord_id = ?
                    LIMIT 1
                    """,
                    (discord_id,),
                ).fetchone()
                if not remaining:
                    conn.execute(
                        f"DELETE FROM {SUMMARY_TABLE} WHERE discord_id = ?",
                        (discord_id,),
                    )
                    conn.commit()
                    continue
            _recompute_summary_for_discord_id(discord_id)
        await ctx.send(
            f"Recorded {len(cleaned)} exchange wallet(s) for `{exchange_name}`."
        )
        await bot._refresh_leaderboards()
    except sqlite3.Error as exc:
        log.exception("Failed to set exchange wallets: %s", exc)
        await ctx.send("Failed to store exchange wallets. Check logs.")


@bot.command(name="removeexchangewallets")
@commands.has_permissions(manage_guild=True)
async def removeexchangewallets(ctx: commands.Context, *wallets: str):
    if not wallets:
        return await ctx.send("Usage: `!removeexchangewallets WALLET1 WALLET2 ...`")
    cleaned = [w.strip() for w in wallets if w.strip()]
    if not cleaned:
        return await ctx.send("No valid wallet addresses provided.")
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            _ensure_exchange_wallets_schema()
            for wallet in cleaned:
                conn.execute(
                    "DELETE FROM exchange_wallets WHERE wallet_address = ?",
                    (wallet,),
                )
            conn.commit()
        await ctx.send(f"Removed {len(cleaned)} exchange wallet(s).")
        await bot._refresh_leaderboards()
    except sqlite3.Error as exc:
        log.exception("Failed to remove exchange wallets: %s", exc)
        await ctx.send("Failed to remove exchange wallets. Check logs.")


@bot.command(name="synccommands")
@commands.has_permissions(manage_guild=True)
async def synccommands(ctx: commands.Context):
    try:
        if ENABLE_DM_COMMANDS:
            if GUILD_ID:
                guild = discord.Object(id=int(GUILD_ID))
                bot.tree.clear_commands(guild=guild)
                await bot.tree.sync(guild=guild)
            bot.tree.clear_commands(guild=None)
            bot.tree.add_command(set_leaderboard_visibility)
            bot.tree.add_command(my_transactions)
            bot.tree.add_command(my_wallets)
            bot.tree.add_command(verify_wallet_otp)
            bot.tree.add_command(verify_status)
            bot.tree.add_command(leaderboard_full)
            bot.tree.add_command(dm_welcome)
            await bot.tree.sync()
            await ctx.send("Synced global slash commands (DMs enabled).")
        else:
            if GUILD_ID:
                guild = discord.Object(id=int(GUILD_ID))
                await bot.tree.sync(guild=guild)
                await ctx.send("Synced guild slash commands.")
            else:
                await ctx.send("DISCORD_GUILD_ID is not set; skipping guild sync.")
    except discord.DiscordException as exc:
        log.exception("Failed to sync commands: %s", exc)
        await ctx.send("Failed to sync slash commands. Check logs.")


@bot.command(name="listcommands")
@commands.has_permissions(manage_guild=True)
async def listcommands(ctx: commands.Context):
    try:
        global_cmds = await bot.tree.fetch_commands()
        global_names = ", ".join(sorted(cmd.name for cmd in global_cmds)) or "none"
        msg = f"Global commands: {len(global_cmds)}\n{global_names}"
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            guild_cmds = await bot.tree.fetch_commands(guild=guild)
            guild_names = ", ".join(sorted(cmd.name for cmd in guild_cmds)) or "none"
            msg += f"\n\nGuild commands: {len(guild_cmds)}\n{guild_names}"
        await ctx.send(f"```\n{msg}\n```")
    except discord.DiscordException as exc:
        log.exception("Failed to list commands: %s", exc)
        await ctx.send("Failed to list commands. Check logs.")


@bot.command(name="tx")
@commands.has_permissions(manage_guild=True)
async def tx_lookup(ctx: commands.Context, target: str = ""):
    if not target:
        return await ctx.send("Usage: `!tx @user` or `!tx WALLET_ADDRESS`")

    wallet_addresses: List[str] = []
    if ctx.message.mentions:
        discord_id = str(ctx.message.mentions[0].id)
        wallet_addresses = _find_wallets_for_discord_id(discord_id)
    else:
        wallet_addresses = [target]

    if not wallet_addresses:
        return await ctx.send("No wallet found for that user.")

    for wallet in wallet_addresses:
        rows = _fetch_transactions_for_wallet(wallet, TX_LOOKUP_LIMIT)
        if not rows:
            await ctx.send(f"No transactions found for `{wallet}`.")
            continue
        lines = []
        for row in rows:
            lines.append(
                f"{row['timestamp']} | {row['amount_ui']} {row['token']} | "
                f"${row['value_usdc']:.6f} | {row['value_fartboy']:.8f} FB"
            )
        header = f"Transactions for `{wallet}` (last {min(len(rows), TX_LOOKUP_LIMIT)}):"
        # Discord message limit safety
        chunk = "\n".join(lines[:50])
        await ctx.send(f"{header}\n```\n{chunk}\n```")


@bot.command(name="addtransaction")
@commands.has_permissions(manage_guild=True)
async def addtransaction(ctx: commands.Context, signature: str = ""):
    if not signature:
        return await ctx.send("Usage: `!addtransaction SIGNATURE [@user]`")
    discord_id = str(ctx.message.mentions[0].id) if ctx.message.mentions else None
    await ctx.send("Processing transaction...")
    total, inserted = await _process_signature(signature.strip(), discord_id=discord_id)
    if total == 0:
        return await ctx.send("No incoming transfers for the war chest found in that signature.")
    if inserted == 0:
        return await ctx.send("Transaction already recorded or pricing unavailable.")
    await ctx.send(f"Recorded {inserted} incoming transfer(s) from that signature.")


@setdonationleaderboard.error
@setdonationleadersize.error
async def setleaderboard_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need **Manage Server** to run this.")
    else:
        log.exception("Failed to set leaderboard: %s", error)
        await ctx.send("Failed to set leaderboard. Check logs.")


@bot.command(name="settarget")
@commands.has_permissions(manage_guild=True)
async def settarget(ctx: commands.Context, amount: str = "", *, name: str = ""):
    """Add a fundraising target. Usage: !settarget <amount> [name]"""
    if not amount:
        return await ctx.send("Usage: `!settarget <amount_usd> [name]`\nExample: `!settarget 25000 Phase 2`")
    try:
        target_amount = float(amount.replace(",", "").replace("$", ""))
        if target_amount <= 0:
            return await ctx.send("Target amount must be positive.")
    except ValueError:
        return await ctx.send("Invalid amount. Use a number like `25000` or `25000.00`.")

    target_name = name.strip() or None
    target_id = _add_target(target_amount, target_name)
    if target_id is None:
        return await ctx.send("Failed to add target. Check logs.")
    label = f" ({target_name})" if target_name else ""
    await ctx.send(f"Target #{target_id} added: **${target_amount:,.2f}**{label}")


@bot.command(name="removetarget")
@commands.has_permissions(manage_guild=True)
async def removetarget(ctx: commands.Context, target_id: str = ""):
    """Remove a fundraising target by ID. Usage: !removetarget <id>"""
    if not target_id:
        return await ctx.send("Usage: `!removetarget <id>`\nUse `!targets` to see all targets and their IDs.")
    try:
        tid = int(target_id)
    except ValueError:
        return await ctx.send("Target ID must be a number.")
    success = _remove_target(tid)
    if success:
        await ctx.send(f"Target #{tid} removed.")
    else:
        await ctx.send(f"Target #{tid} not found or already removed.")


@bot.command(name="targets")
async def targets_cmd(ctx: commands.Context):
    """Show all active fundraising targets."""
    targets = _fetch_targets()
    total_raised = _fetch_total_donations_usd()
    _update_target_completion(total_raised)

    if not targets:
        return await ctx.send("No active targets set. Admins can use `!settarget <amount> [name]`.")

    lines = [f"**Total Raised: ${total_raised:,.2f}**\n"]
    for t in targets:
        progress = (total_raised / t["target_amount"] * 100) if t["target_amount"] > 0 else 0
        progress = min(100.0, progress)
        status = "completed" if t["completed_at"] else f"{progress:.1f}%"
        label = f' "{t["target_name"]}"' if t["target_name"] else ""
        lines.append(
            f"#{t['id']}: **${t['target_amount']:,.2f}**{label} — {status}"
        )

    next_target = _fetch_next_target(total_raised)
    if next_target:
        progress = (total_raised / next_target["target_amount"] * 100) if next_target["target_amount"] > 0 else 0
        label = f' ({next_target["target_name"]})' if next_target.get("target_name") else ""
        lines.append(f"\n**Next target:** ${next_target['target_amount']:,.2f}{label} — {min(100.0, progress):.1f}%")

    await ctx.send("\n".join(lines))


@settarget.error
@removetarget.error
async def target_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need **Manage Server** to run this.")
    else:
        log.exception("Target command error: %s", error)
        await ctx.send("Failed to execute target command. Check logs.")


def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("ERROR: Missing DISCORD_BOT_TOKEN in environment.")

    # Start API server in-process if API_KEY is configured.
    api_key = os.getenv("API_KEY")
    if api_key:
        import threading
        import uvicorn
        from api_server import create_api_app

        api_app = create_api_app()
        api_port = int(os.getenv("API_PORT", "8000"))
        api_host = os.getenv("API_HOST", "0.0.0.0")

        def run_api():
            uvicorn.run(api_app, host=api_host, port=api_port, log_level="info")

        api_thread = threading.Thread(target=run_api, daemon=True)
        api_thread.start()
        log.info("API server started on %s:%s", api_host, api_port)
    else:
        log.info("API_KEY not set; API server disabled.")

    bot.run(token)


if __name__ == "__main__":
    main()
