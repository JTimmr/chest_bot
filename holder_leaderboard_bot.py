#!/usr/bin/env python3
"""
Discord bot: maintains a permanent donation leaderboard message.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
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
    _peek_assigned_otp,
    _match_assigned_otp,
    _record_used_otp,
    _check_strict_gate,
    _insert_verification_rejection,
    recompute_summary_for_discord_id,
    recompute_summary_for_wallet,
    recompute_all_verified_summaries,
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


def _ensure_donation_tiers_schema() -> None:
    try:
        with _connect_db(STATE_DB) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS donation_tiers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    min_usd REAL NOT NULL,
                    emoji TEXT NOT NULL,
                    role_name TEXT NOT NULL,
                    order_index INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            existing_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(donation_tiers)")
            }
            if "role_id" not in existing_cols:
                conn.execute(
                    "ALTER TABLE donation_tiers ADD COLUMN role_id TEXT"
                )
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to ensure donation_tiers schema: %s", exc)


def _ensure_donor_config_schema() -> None:
    try:
        with _connect_db(STATE_DB) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS donor_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to ensure donor_config schema: %s", exc)


def _ensure_user_threshold_resolution_schema() -> None:
    try:
        with _connect_db(STATE_DB) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_threshold_resolution (
                    discord_id TEXT PRIMARY KEY,
                    resolution TEXT NOT NULL CHECK (resolution IN ('force_met', 'force_not_met')),
                    updated_at TEXT NOT NULL,
                    actor_discord_id TEXT
                )
                """
            )
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to ensure user_threshold_resolution schema: %s", exc)


def _ensure_verification_rejections_schema() -> None:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS verification_rejections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id TEXT,
                    sender_wallet TEXT,
                    signature TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_vr_discord_created
                ON verification_rejections (discord_id, created_at)
                """
            )
            conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to ensure verification_rejections schema: %s", exc)


def _get_donor_config(key: str) -> str | None:
    try:
        with _connect_db(STATE_DB) as conn:
            row = conn.execute(
                "SELECT value FROM donor_config WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None
    except sqlite3.Error as exc:
        log.error("Failed to read donor_config key %s: %s", key, exc)
        return None


def _set_donor_config(key: str, value: str) -> bool:
    try:
        with _connect_db(STATE_DB) as conn:
            conn.execute(
                """
                INSERT INTO donor_config (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            conn.commit()
        return True
    except sqlite3.Error as exc:
        log.error("Failed to set donor_config key %s: %s", key, exc)
        return False


def _add_tier(min_usd: float, emoji: str, role_name: str, role_id: str | None = None) -> int | None:
    try:
        with _connect_db(STATE_DB) as conn:
            row = conn.execute(
                "SELECT MAX(order_index) FROM donation_tiers"
            ).fetchone()
            next_order = int(row[0] or 0) + 1 if row and row[0] is not None else 1
            cur = conn.execute(
                """
                INSERT INTO donation_tiers (min_usd, emoji, role_name, role_id, order_index, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (min_usd, emoji, role_name, role_id, next_order),
            )
            conn.commit()
            return cur.lastrowid
    except sqlite3.Error as exc:
        log.error("Failed to add tier: %s", exc)
        return None


def _remove_tier(tier_id: int) -> bool:
    try:
        with _connect_db(STATE_DB) as conn:
            cur = conn.execute(
                "UPDATE donation_tiers SET is_active = 0 WHERE id = ?",
                (tier_id,),
            )
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.Error as exc:
        log.error("Failed to remove tier: %s", exc)
        return False


def _lookup_guild_role_by_name(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    """Find a guild role by name.

    Supports:
    - exact role names
    - names beginning with '@'
    - fallback matching with/without '@'
    - case-insensitive matching

    NEVER creates roles.
    """

    if not name:
        return None

    raw = str(name).strip()

    # Exact match first.
    role = discord.utils.get(guild.roles, name=raw)
    if role:
        return role

    # Try variants with/without @.
    if raw.startswith("@"):
        possible_names = [raw, raw[1:]]
    else:
        possible_names = [raw, f"@{raw}"]

    for candidate in possible_names:
        role = discord.utils.get(guild.roles, name=candidate)
        if role:
            return role

    # Case-insensitive fallback.
    lowered = {p.lower() for p in possible_names}

    for role in guild.roles:
        if role.name.lower() in lowered:
            return role

    return None


def _canonical_role_name(guild: discord.Guild, stored: str) -> str:
    """Resolve a stored role reference to the guild's canonical role name."""
    mention_match = re.match(r"<@&(\d+)>$", stored.strip())
    if mention_match:
        role = guild.get_role(int(mention_match.group(1)))
        return role.name if role else stored

    role = _lookup_guild_role_by_name(guild, stored)
    return role.name if role else stored


def _resolve_role_by_id_or_name(
    guild: discord.Guild, role_id: str | None, role_name: str
) -> Optional[discord.Role]:
    """Resolve a role using stored ID first (exact), falling back to name lookup."""
    if role_id:
        try:
            role = guild.get_role(int(role_id))
            if role:
                return role
        except (ValueError, TypeError):
            pass
    return _lookup_guild_role_by_name(guild, role_name)


def _resolve_role_input(guild: discord.Guild, raw: str) -> Optional[str]:
    """Resolve user role input safely.

    Supports:
    - <@&ROLE_ID>
    - @RoleName
    - RoleName

    Returns the ACTUAL canonical guild role name.
    """

    if not raw:
        return None

    raw = raw.strip()

    # Discord role mention.
    mention_match = re.match(r"<@&(\d+)>$", raw)
    if mention_match:
        role = guild.get_role(int(mention_match.group(1)))
        return role.name if role else None

    role = _lookup_guild_role_by_name(guild, raw)

    if role:
        return role.name

    return None


def _fetch_tiers() -> List[Dict]:
    try:
        with _connect_db(STATE_DB) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(donation_tiers)")}
            has_role_id = "role_id" in cols
            if has_role_id:
                rows = conn.execute(
                    """
                    SELECT id, min_usd, emoji, role_name, order_index, role_id
                    FROM donation_tiers
                    WHERE is_active = 1
                    ORDER BY min_usd ASC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, min_usd, emoji, role_name, order_index
                    FROM donation_tiers
                    WHERE is_active = 1
                    ORDER BY min_usd ASC
                    """
                ).fetchall()
        return [
            {
                "id": r[0],
                "min_usd": float(r[1]),
                "emoji": r[2],
                "role_name": r[3],
                "order_index": r[4],
                "role_id": r[5] if has_role_id and len(r) > 5 else None,
            }
            for r in rows
        ]
    except sqlite3.Error as exc:
        log.error("Failed to fetch tiers: %s", exc)
        return []


def _get_user_threshold_resolution(discord_id: str) -> str | None:
    """Return 'force_met', 'force_not_met', or None (no override)."""
    try:
        with _connect_db(STATE_DB) as conn:
            row = conn.execute(
                "SELECT resolution FROM user_threshold_resolution WHERE discord_id = ?",
                (discord_id,),
            ).fetchone()
            return row[0] if row else None
    except sqlite3.Error as exc:
        log.error("Failed to read user_threshold_resolution for %s: %s", discord_id, exc)
        return None


def _check_base_eligibility(discord_id: str) -> bool:
    """Check if a verified user qualifies for the base donor tier.

    Evaluation order:
      1. user_threshold_resolution override (force_met / force_not_met)
      2. Automatic: any linked wallet donated >= 1% of snapshot holdings
    """
    resolution = _get_user_threshold_resolution(discord_id)
    if resolution == "force_not_met":
        return False
    if resolution == "force_met":
        return True

    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            wallets = conn.execute(
                f"""
                SELECT wallet_address, amount_fartboy, donated_fartboy
                FROM {SNAPSHOT_TABLE}
                WHERE discord_id = ?
                """,
                (discord_id,),
            ).fetchall()
            if not wallets:
                return False

            for _wallet, amount_fartboy, donated_fartboy in wallets:
                amount = float(amount_fartboy or 0)
                donated = float(donated_fartboy or 0)
                if amount > 0 and donated >= amount * 0.01:
                    return True

            return False
    except sqlite3.Error as exc:
        log.error("Failed to check base eligibility for %s: %s", discord_id, exc)
        return False


def _get_user_donated_usd(discord_id: str) -> float:
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            row = conn.execute(
                f"SELECT total_donated_usd FROM {SUMMARY_TABLE} WHERE discord_id = ?",
                (discord_id,),
            ).fetchone()
        return float(row[0] or 0) if row else 0.0
    except sqlite3.Error as exc:
        log.error("Failed to get donated usd for %s: %s", discord_id, exc)
        return 0.0


def _all_tier_emojis() -> List[str]:
    return [t["emoji"] for t in _fetch_tiers()]


def _strip_tier_emoji(name: str, known_emojis: List[str]) -> str:
    """Remove any known tier emoji prefix from a display name."""
    stripped = name
    for emoji in known_emojis:
        if stripped.startswith(emoji + " "):
            stripped = stripped[len(emoji) + 1:]
        elif stripped.startswith(emoji):
            stripped = stripped[len(emoji):]
    return stripped.strip()


def _build_tier_nickname(base_name: str, emoji: str | None, known_emojis: List[str]) -> str:
    clean = _strip_tier_emoji(base_name, known_emojis)
    if not emoji:
        return clean[:32]
    candidate = f"{emoji} {clean}"
    if len(candidate) > 32:
        max_name = 32 - len(emoji) - 1
        candidate = f"{emoji} {clean[:max_name]}"
    return candidate


async def sync_donor_roles(guild: discord.Guild) -> Dict[str, int]:
    """Sync donor roles and nickname emojis for all verified users.

    Returns a dict with counts: updated, skipped_base, skipped_tier,
    failed_permission, skipped_nick.
    """
    counts = {
        "updated": 0,
        "skipped_base": 0,
        "skipped_tier": 0,
        "failed_permission": 0,
        "skipped_nick": 0,
        "processed": 0,
        "debug": [],
    }
    tiers = _fetch_tiers()
    base_role_name = _get_donor_config("base_role_name")
    base_role_id = _get_donor_config("base_role_id")
    if not tiers or not base_role_name:
        return counts

    base_role = _resolve_role_by_id_or_name(guild, base_role_id, base_role_name)
    if not base_role:
        log.warning("Base donor role '%s' (id=%s) could not be resolved; skipping sync.", base_role_name, base_role_id)
        return counts

    known_emojis = [t["emoji"] for t in tiers]

    for t in tiers:
        resolved = _resolve_role_by_id_or_name(guild, t.get("role_id"), t["role_name"])
        t["_resolved_role"] = resolved
        if resolved:
            t["role_name"] = resolved.name

    tiers_desc = sorted(tiers, key=lambda t: t["min_usd"], reverse=True)

    tier_roles = set()
    for t in tiers:
        if t.get("_resolved_role"):
            tier_roles.add(t["_resolved_role"])

    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            users = conn.execute(
                f"SELECT discord_id, total_donated_usd FROM {SUMMARY_TABLE} WHERE discord_id IS NOT NULL"
            ).fetchall()
    except sqlite3.Error as exc:
        log.error("Failed to load verified users for role sync: %s", exc)
        return counts

    for discord_id, total_donated_usd in users:
        discord_id = str(discord_id)
        donated_usd = float(total_donated_usd or 0)

        if not _check_base_eligibility(discord_id):
            counts["skipped_base"] += 1
            continue

        target_tier = None
        for t in tiers_desc:
            if donated_usd >= t["min_usd"]:
                target_tier = t
                break

        if not target_tier:
            counts["skipped_tier"] += 1
            continue

        target_tier_role = target_tier.get("_resolved_role")
        if not target_tier_role:
            counts["skipped_tier"] += 1
            continue

        try:
            member = guild.get_member(int(discord_id))
            if not member:
                try:
                    member = await guild.fetch_member(int(discord_id))
                except discord.HTTPException:
                    counts["failed_permission"] += 1
                    continue
        except (ValueError, discord.HTTPException):
            counts["failed_permission"] += 1
            continue

        counts["processed"] += 1

        roles_to_add = set()
        roles_to_remove = set()

        has_base = base_role in member.roles
        has_tier = target_tier_role in member.roles
        if not has_base:
            roles_to_add.add(base_role)
        if not has_tier:
            roles_to_add.add(target_tier_role)

        for tr in tier_roles:
            if tr != target_tier_role and tr in member.roles:
                roles_to_remove.add(tr)

        member_name = str(member)
        donated_str = f"${donated_usd:,.2f}"
        tier_name = target_tier_role.name
        status_parts = [f"**{member_name}** ({discord_id}): {donated_str} → {tier_name}"]
        if not roles_to_add and not roles_to_remove:
            status_parts.append("roles OK")
        else:
            if roles_to_add:
                status_parts.append(f"+{', '.join(r.name for r in roles_to_add)}")
            if roles_to_remove:
                status_parts.append(f"-{', '.join(r.name for r in roles_to_remove)}")
        counts["debug"].append(" | ".join(status_parts))

        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Donor tier update")
            if roles_to_add:
                log.info(
                    "Adding roles %s to %s (member has %d roles, is_owner=%s)",
                    [r.name for r in roles_to_add],
                    discord_id,
                    len(member.roles),
                    member.id == guild.owner_id,
                )
                await member.add_roles(*roles_to_add, reason="Donor tier update")
            if roles_to_add or roles_to_remove:
                counts["updated"] += 1
        except discord.HTTPException as exc:
            log.warning("Failed to update roles for %s: %s", discord_id, exc)
            counts["failed_permission"] += 1

        target_nick = _build_tier_nickname(
            member.display_name, target_tier["emoji"], known_emojis
        )
        if member.nick != target_nick:
            try:
                await member.edit(nick=target_nick, reason="Donor tier emoji")
            except discord.HTTPException as exc:
                log.warning("Failed to set nickname for %s: %s", discord_id, exc)
                counts["skipped_nick"] += 1

        try:
            with _connect_db(SNAPSHOT_DB) as conn:
                conn.execute(
                    f"""
                    UPDATE {SUMMARY_TABLE}
                    SET roles = ?
                    WHERE discord_id = ?
                    """,
                    (f"{base_role.name},{target_tier['role_name']}", discord_id),
                )
                conn.commit()
        except sqlite3.Error:
            pass

        await asyncio.sleep(0.5)
    return counts


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


def _sync_snapshot_donations(
    snapshot_conn: sqlite3.Connection,
    tx_conn: sqlite3.Connection,
    snapshot_table: str,
    tx_table: str,
    sender_wallet: str,
) -> None:
    """Ensure snapshot donated_fartboy/donated_usd reflect tx aggregates.

    Called when !addtransaction re-attributes an existing tx row so
    _apply_donations was not run.  Sets snapshot values to the max of
    existing snapshot values and the tx-table aggregates for the wallet.
    """
    try:
        tx_row = tx_conn.execute(
            f"""
            SELECT COALESCE(SUM(value_fartboy), 0),
                   COALESCE(SUM(value_usdc), 0)
            FROM {tx_table}
            WHERE sender_wallet = ?
            """,
            (sender_wallet,),
        ).fetchone()
        if not tx_row:
            return
        tx_fartboy = float(tx_row[0] or 0)
        tx_usd = float(tx_row[1] or 0)

        snap_row = snapshot_conn.execute(
            f"""
            SELECT donated_fartboy, donated_usd
            FROM {snapshot_table}
            WHERE wallet_address = ?
            """,
            (sender_wallet,),
        ).fetchone()
        new_fartboy = tx_fartboy
        new_usd = tx_usd
        if snap_row:
            snap_fartboy = float(snap_row[0] or 0)
            snap_usd = float(snap_row[1] or 0)
            new_fartboy = max(snap_fartboy, tx_fartboy)
            new_usd = max(snap_usd, tx_usd)
            if new_fartboy > snap_fartboy or new_usd > snap_usd:
                snapshot_conn.execute(
                    f"""
                    UPDATE {snapshot_table}
                    SET donated_fartboy = ?, donated_usd = ?
                    WHERE wallet_address = ?
                    """,
                    (new_fartboy, new_usd, sender_wallet),
                )
                snapshot_conn.commit()
        elif new_fartboy > 0 or new_usd > 0:
            snapshot_conn.execute(
                f"""
                INSERT INTO {snapshot_table}
                    (wallet_address, amount_fartboy, donated_fartboy, donated_usd)
                VALUES (?, 0, ?, ?)
                """,
                (sender_wallet, new_fartboy, new_usd),
            )
            snapshot_conn.commit()
    except sqlite3.Error as exc:
        log.warning("Failed to sync snapshot donations for %s: %s", sender_wallet, exc)


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
    attribution_discord_id = discord_id
    exchange_wallets = _get_exchange_wallets()
    with sqlite3.connect(TX_DB) as tx_conn, sqlite3.connect(SNAPSHOT_DB) as snapshot_conn:
        _init_transactions_db(tx_conn, TX_TABLE)
        _init_snapshot_db(snapshot_conn, SNAPSHOT_TABLE)
        _ensure_otp_registry(snapshot_conn)
        _expire_otps(snapshot_conn)
        for row, value_usdc, value_fartboy in computed:
            if row.get("token") != "FARTBOY" and value_usdc < 0.01:
                continue
            row_discord_id = discord_id if discord_id else None
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
                recompute_summary_for_wallet(
                    row["sender_wallet"],
                    snapshot_db=SNAPSHOT_DB,
                    snapshot_table=SNAPSHOT_TABLE,
                    tx_db_path=TX_DB,
                    tx_table=TX_TABLE,
                )
            elif row_discord_id:
                _sync_snapshot_donations(
                    snapshot_conn,
                    tx_conn,
                    SNAPSHOT_TABLE,
                    TX_TABLE,
                    row["sender_wallet"],
                )
                recompute_summary_for_wallet(
                    row["sender_wallet"],
                    snapshot_db=SNAPSHOT_DB,
                    snapshot_table=SNAPSHOT_TABLE,
                    tx_db_path=TX_DB,
                    tx_table=TX_TABLE,
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
        # Mirror: incoming_tracker.py IncomingTracker.run_once has the same
        # peek/strict/rejection logic and must stay in sync.
        verification_order = _get_donor_config("verification_order") or "strict"
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
                peek = _peek_assigned_otp(snapshot_conn, otp_value, tick)
                if peek is None:
                    _record_used_otp(
                        snapshot_conn,
                        otp_value,
                        tick,
                        row["signature"],
                        row["sender_wallet"],
                    )
                    continue
                otp_discord_id, otp_discord_name = peek
                if verification_order == "strict":
                    rejection = _check_strict_gate(
                        snapshot_conn,
                        SNAPSHOT_TABLE,
                        row["sender_wallet"],
                    )
                    if rejection:
                        _insert_verification_rejection(
                            snapshot_conn,
                            otp_discord_id,
                            row["sender_wallet"],
                            row["signature"],
                            rejection,
                        )
                        break
                matched = _match_assigned_otp(
                    snapshot_conn,
                    otp_value,
                    tick,
                    row["signature"],
                    row["sender_wallet"],
                )
                if matched:
                    snapshot_conn.execute(
                        f"""
                        UPDATE {SNAPSHOT_TABLE}
                        SET discord_id = ?, discord_name = ?, on_leaderboard = 0
                        WHERE wallet_address = ?
                          AND (discord_id IS NULL OR discord_id = ?)
                        """,
                        (otp_discord_id, otp_discord_name, row["sender_wallet"], otp_discord_id),
                    )
                    snapshot_conn.commit()
                    recompute_summary_for_discord_id(otp_discord_id)
                break
    if attribution_discord_id:
        recompute_summary_for_discord_id(
            attribution_discord_id,
            snapshot_db=SNAPSHOT_DB,
            snapshot_table=SNAPSHOT_TABLE,
            tx_db_path=TX_DB,
            tx_table=TX_TABLE,
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


def _get_snapshot_discord_id_for_wallet(wallet: str) -> str | None:
    """Return linked discord_id for wallet in snapshot, or None if missing/unlinked."""
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
        if not row or not row[0]:
            return None
        return str(row[0])
    except sqlite3.Error as exc:
        log.error("Failed to read snapshot link for wallet %s: %s", wallet, exc)
        return None


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
            recompute_summary_for_discord_id(discord_id)
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
    return f"`{bar}` {pct:.2f}%"


def _render_target_field(total_raised: float) -> str | None:
    """Build the target/milestone progress string for the embed."""
    _update_target_completion(total_raised)
    next_target = _fetch_next_target(total_raised)
    if not next_target:
        return None
    amount = next_target["target_amount"]
    name = next_target.get("target_name")
    bar = _render_progress_bar(total_raised, amount)
    label = f'{name} — ' if name else ""
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


class SignatureModal(discord.ui.Modal, title="Paste transaction signature"):
    signature_input = discord.ui.TextInput(
        label="Transaction signature",
        placeholder="Paste the full tx signature from your wallet or Solscan",
        style=discord.TextStyle.short,
        required=True,
        min_length=40,
        max_length=120,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        sig = self.signature_input.value.strip()
        discord_id = str(interaction.user.id)
        try:
            total, inserted = await _process_signature(sig)
        except Exception as exc:
            log.warning("Modal signature processing failed for %s: %s", sig[:16], exc)
            return await interaction.followup.send(
                "Something went wrong processing that signature. Please double-check it.",
                ephemeral=True,
            )
        if total == 0:
            return await interaction.followup.send(
                "No incoming transfers to the war chest found in that transaction. "
                "Make sure you pasted the correct signature.",
                ephemeral=True,
            )

        # Re-run tracker + check verification result
        if bot._tracker:
            try:
                await bot._tracker.run_once()
            except Exception:
                pass

        recompute_summary_for_discord_id(discord_id)
        verified_wallets = _find_wallets_for_discord_id(discord_id)
        if verified_wallets:
            short = ", ".join(
                f"{w[:6]}...{w[-4:]}" if len(w) > 14 else w for w in verified_wallets
            )
            await interaction.followup.send(
                f"**Verified!** Your wallet is now linked: {short}",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Transaction processed ({total} transfer(s) found). "
                f"OTP matching will run automatically — check back with **Verify status** shortly.",
                ephemeral=True,
            )


class SignatureSubmitView(discord.ui.View):
    """Ephemeral view with a button to open the signature modal."""

    def __init__(self) -> None:
        super().__init__(timeout=300)

    @discord.ui.button(
        label="Paste tx signature",
        style=discord.ButtonStyle.primary,
    )
    async def paste_sig_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(SignatureModal())


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
        await verify_wallet_otp.callback(interaction)

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
        label="Become visible",
        style=discord.ButtonStyle.success,
        custom_id="become_visible_button",
        row=2,
    )
    async def become_visible_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        discord_id = str(interaction.user.id)
        if not _find_wallets_for_discord_id(discord_id):
            return await interaction.response.send_message(
                "No verified wallet found. Use the Verify wallet button first.",
                ephemeral=True,
            )
        if _is_leaderboard_visible(discord_id):
            return await interaction.response.send_message(
                "You are already visible on the leaderboard.",
                ephemeral=True,
            )
        _update_visibility_by_discord(discord_id, str(interaction.user), 1)
        _set_summary_visibility(discord_id, 1)
        recompute_summary_for_discord_id(discord_id)
        await interaction.response.send_message(
            "Your name is now **visible** on the leaderboard. You can change this anytime.",
            ephemeral=True,
        )
        await bot._refresh_leaderboards()

    @discord.ui.button(
        label="Become anonymous",
        style=discord.ButtonStyle.danger,
        custom_id="become_anonymous_button",
        row=2,
    )
    async def become_anonymous_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        discord_id = str(interaction.user.id)
        if not _find_wallets_for_discord_id(discord_id):
            return await interaction.response.send_message(
                "No verified wallet found. Use the Verify wallet button first.",
                ephemeral=True,
            )
        if not _is_leaderboard_visible(discord_id):
            return await interaction.response.send_message(
                "You are already anonymous on the leaderboard.",
                ephemeral=True,
            )
        _update_visibility_by_discord(discord_id, str(interaction.user), 0)
        _set_summary_visibility(discord_id, 0)
        recompute_summary_for_discord_id(discord_id)
        await interaction.response.send_message(
            "You are now **anonymous** on the leaderboard. Your donations show as 'Anonymous donor'.",
            ephemeral=True,
        )
        await bot._refresh_leaderboards()

    @discord.ui.button(
        label="Full leaderboard",
        style=discord.ButtonStyle.secondary,
        custom_id="full_leaderboard_button",
        row=3,
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
        _ensure_live_state_schema()
        _ensure_otp_schema()
        _ensure_exchange_wallets_schema()
        _ensure_targets_schema()
        _ensure_donation_tiers_schema()
        _ensure_donor_config_schema()
        _ensure_user_threshold_resolution_schema()
        _ensure_verification_rejections_schema()
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
                    state_db=STATE_DB,
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
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", GUILD_ID)
        else:
            self.tree.add_command(set_leaderboard_visibility)
            self.tree.add_command(my_transactions)
            self.tree.add_command(my_wallets)
            self.tree.add_command(verify_wallet_otp)
            self.tree.add_command(verify_status)
            self.tree.add_command(leaderboard_full)
            await self.tree.sync()
        self.update_leaderboard.start()
        self.watch_snapshot_changes.start()
        self.track_incoming.start()

    @tasks.loop(seconds=UPDATE_SECONDS)
    async def update_leaderboard(self) -> None:
        try:
            await self._refresh_leaderboards()
        except Exception as exc:
            log.exception("Leaderboard refresh failed: %s", exc)

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
            except discord.HTTPException:
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
        try:
            if mtime > self._last_snapshot_mtime:
                self._last_snapshot_mtime = mtime
                await self._refresh_leaderboards()
        except Exception as exc:
            log.exception("Snapshot-triggered refresh failed: %s", exc)

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
            for w in wallets:
                recompute_summary_for_wallet(
                    w,
                    snapshot_db=SNAPSHOT_DB,
                    snapshot_table=SNAPSHOT_TABLE,
                    tx_db_path=TX_DB,
                    tx_table=TX_TABLE,
                )
            for did in verified_ids:
                recompute_summary_for_discord_id(
                    did,
                    snapshot_db=SNAPSHOT_DB,
                    snapshot_table=SNAPSHOT_TABLE,
                    tx_db_path=TX_DB,
                    tx_table=TX_TABLE,
                )
            if (count > 0 or verified_ids) and GUILD_ID:
                guild = self.get_guild(int(GUILD_ID))
                if guild:
                    await sync_donor_roles(guild)
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
intents.members = True
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
    recompute_summary_for_discord_id(str(interaction.user.id))
    await bot._refresh_leaderboards()


@bot.command(name="checkwallet")
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
    discord_id_str = str(interaction.user.id)
    wallets = _find_wallets_for_discord_id(discord_id_str)
    wallet_rows = _fetch_transactions_for_wallets(wallets, TX_LOOKUP_LIMIT)
    attributed_rows = _fetch_transactions_for_discord_id(discord_id_str, TX_LOOKUP_LIMIT)
    seen_keys: set = set()
    merged: list = []
    for row in wallet_rows:
        key = (row.get("timestamp"), row.get("wallet"), row.get("token"), row.get("amount_ui"))
        if key not in seen_keys:
            seen_keys.add(key)
            merged.append(row)
    for row in attributed_rows:
        key = (row.get("timestamp"), row.get("wallet"), row.get("token"), row.get("amount_ui"))
        if key not in seen_keys:
            seen_keys.add(key)
            merged.append(row)
    merged.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    if not merged:
        return await interaction.response.send_message(
            "No transactions found for your wallet.",
            ephemeral=True,
        )
    total_usd = 0.0
    total_fartboy = 0.0
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
    for row in merged[:50]:
        lines.append(
            f"{row['timestamp']} | {row['amount_ui']} {row['token']} | "
            f"${row['value_usdc']:.6f}"
        )
        total_usd += float(row["value_usdc"] or 0)
        total_fartboy += float(row.get("value_fartboy") or 0)
    body = "\n".join(lines)
    total_pct = 0.0
    if total_holdings > 0:
        total_pct = (total_fartboy / total_holdings) * 100.0
    await interaction.response.send_message(
        f"Transactions from your linked wallet(s):\n```\n{body}\n```\n"
        f"Total donated: ${total_usd:,.2f} | {total_pct:.2f}% toward 1% FARTBOY requirement",
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
    total_donated_fartboy = 0.0
    total_holdings = 0.0
    for wallet in wallets:
        try:
            with _connect_db(SNAPSHOT_DB) as conn:
                row = conn.execute(
                    f"""
                    SELECT donated_usd, amount_fartboy, donated_fartboy
                    FROM {SNAPSHOT_TABLE}
                    WHERE wallet_address = ?
                    """,
                    (wallet,),
                ).fetchone()
            donated_usd = float(row[0] or 0) if row else 0.0
            holdings = float(row[1] or 0) if row else 0.0
            donated_fb = float(row[2] or 0) if row else 0.0
        except sqlite3.Error:
            donated_usd = 0.0
            holdings = 0.0
            donated_fb = 0.0
        total_usd += donated_usd
        total_donated_fartboy += donated_fb
        total_holdings += holdings

        pct = 0.0
        if holdings > 0:
            pct = (donated_fb / holdings) * 100.0
        short_wallet = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 14 else wallet
        lines.append(f"{short_wallet} | ${donated_usd:,.2f} | {pct:.2f}% toward 1%")

    total_pct = 0.0
    if total_holdings > 0:
        total_pct = (total_donated_fartboy / total_holdings) * 100.0
    lines.append(f"Total | ${total_usd:,.2f} | {total_pct:.2f}% toward 1%")
    body = "\n".join(lines)
    await interaction.response.send_message(
        f"Your verified wallets and donations:\n```\n{body}\n```",
        ephemeral=True,
    )


async def _log_otp(user: discord.User | discord.Member, otp_value: str) -> None:
    """Send OTP assignment to the admin log channel, if configured."""
    channel_id = _get_donor_config("otp_log_channel_id")
    if not channel_id:
        return
    try:
        channel = bot.get_channel(int(channel_id))
        if channel and isinstance(channel, discord.TextChannel):
            await channel.send(f"OTP assigned: **{user}** → `{otp_value}`")
    except Exception as exc:
        log.warning("Failed to send OTP log: %s", exc)


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
    await _log_otp(interaction.user, otp_value)
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
        "**Important:** You must donate at least **1% of your FARTBOY snapshot balance** to the "
        "war chest **before** sending the OTP. If you send the OTP too early, verification will "
        "be rejected — you can try again after donating more.\n\n"
        "After sending, click **Verify status** to trigger verification.\n"
        "If something doesn't work or you need help, ask the mods."
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
    description="Show your verification status. Optionally provide your tx signature.",
)
@app_commands.describe(
    signature="(Optional) Paste your transaction signature if auto-detection missed it."
)
async def verify_status(interaction: discord.Interaction, signature: str | None = None):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(interaction.user.id)

    # If the user provided a tx signature, process it directly via getTransaction
    # (bypasses getSignaturesForAddress entirely — always works for on-chain txs).
    if signature:
        signature = signature.strip()
        try:
            total, inserted = await _process_signature(signature)
            if total > 0:
                log.info(
                    "User %s provided signature %s — processed %s transfer(s).",
                    discord_id, signature[:16], total,
                )
        except Exception as exc:
            log.warning("Direct signature processing failed for %s: %s", signature[:16], exc)

    if bot._tracker:
        try:
            count, wallets, verified_ids = await bot._tracker.run_once()
            discord_ids = {
                did for w in wallets if (did := _discord_id_for_wallet(w))
            }
            for did in discord_ids:
                recompute_summary_for_discord_id(did)
            if count:
                log.info("On-demand tracker run found %s new transactions.", count)
        except Exception as exc:
            log.warning("On-demand tracker run failed: %s", exc)

    recompute_summary_for_discord_id(
        discord_id,
        snapshot_db=SNAPSHOT_DB,
        snapshot_table=SNAPSHOT_TABLE,
        tx_db_path=TX_DB,
        tx_table=TX_TABLE,
    )

    verified_wallets = _find_wallets_for_discord_id(discord_id)

    pending = None
    rows = []
    rejections = []
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
                (discord_id,),
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT otp_value, tick_size, status, assigned_at, used_at, tx_signature
                FROM {OTP_TABLE}
                WHERE assigned_to_discord_id = ?
                ORDER BY assigned_at DESC
                """,
                (discord_id,),
            ).fetchall()
            rejections = conn.execute(
                """
                SELECT reason, sender_wallet, created_at
                FROM verification_rejections
                WHERE discord_id = ?
                ORDER BY created_at DESC
                LIMIT 5
                """,
                (discord_id,),
            ).fetchall()
    except sqlite3.Error:
        pass

    if verified_wallets:
        short = ", ".join(
            f"{w[:6]}...{w[-4:]}" if len(w) > 14 else w for w in verified_wallets
        )
        msg = f"**Verified** — your wallet(s) are linked: {short}"
        if not _is_leaderboard_visible(discord_id):
            msg += "\n\n" + _reminder_text()
        return await interaction.followup.send(msg, ephemeral=True)

    if not rows:
        message = "**No verification started** — use `/verifywallet` to begin."
        if not _is_leaderboard_visible(discord_id):
            message += "\n\n" + _reminder_text()
        return await interaction.followup.send(message, ephemeral=True)

    has_recent_rejection = bool(rejections)
    rejection_reason_text = ""
    fartboy_progress_text = ""

    if has_recent_rejection:
        reason, rej_wallet, _created = rejections[0]
        reason_map = {
            "strict_below_1pct": "your donation from that wallet has not yet reached 1% of your FARTBOY snapshot holding",
            "strict_no_snapshot_balance": "the sending wallet was not found in the FARTBOY snapshot or has zero balance",
        }
        rejection_reason_text = reason_map.get(reason, reason)

        if rej_wallet:
            try:
                with _connect_db(SNAPSHOT_DB) as conn:
                    snap = conn.execute(
                        f"""
                        SELECT amount_fartboy, donated_fartboy
                        FROM {SNAPSHOT_TABLE}
                        WHERE wallet_address = ?
                        """,
                        (rej_wallet,),
                    ).fetchone()
                    if snap:
                        holding = float(snap[0] or 0)
                        donated = float(snap[1] or 0)
                        if holding > 0:
                            pct = (donated / holding) * 100.0
                            needed = holding * 0.01
                            fartboy_progress_text = (
                                f"Progress: {donated:,.2f} / {needed:,.2f} FARTBOY "
                                f"({pct:.2f}% of 1% requirement)"
                            )
            except sqlite3.Error:
                pass

    war_chest = os.getenv("WARCHEST_ADDRESS", "")

    if pending:
        otp_value, tick_size, assigned_at = pending
        remaining = _format_remaining(assigned_at)

        if has_recent_rejection:
            parts = [
                f"**Verification attempt received — not completed**",
                f"Reason: {rejection_reason_text}.",
            ]
            if fartboy_progress_text:
                parts.append(fartboy_progress_text)
            parts.append(
                f"\nYour OTP is still active (expires in {remaining}). "
                f"Donate more FARTBOY to the war chest from the same wallet, "
                f"then re-send the same OTP amount to verify."
            )
            parts.append(f"OTP amount: `{otp_value}`")
            parts.append(f"War chest: `{war_chest}`")
            parts.append(
                f"\nAlready sent? If auto-detection missed it, paste your tx signature below."
            )
        else:
            parts = [
                f"**Verification pending** — send the OTP amount to the war chest.",
                f"OTP amount: `{otp_value}`",
                f"War chest: `{war_chest}`",
                f"Expires in {remaining}.",
                f"\nAlready sent? If auto-detection missed it, paste your tx signature below.",
            ]
        await interaction.followup.send(
            "\n".join(parts), ephemeral=True, view=SignatureSubmitView()
        )
    elif has_recent_rejection:
        parts = [
            f"**Verification attempt received — not completed**",
            f"Reason: {rejection_reason_text}.",
        ]
        if fartboy_progress_text:
            parts.append(fartboy_progress_text)
        parts.append(
            "\nYour previous OTP has expired. Use `/verifywallet` to get a new one "
            "after donating enough to meet the 1% requirement."
        )
        await interaction.followup.send("\n".join(parts), ephemeral=True)
    else:
        await interaction.followup.send(
            "**No active OTP** — your previous OTP(s) have expired or been used. "
            "Use `/verifywallet` to start again.",
            ephemeral=True,
        )

    status_labels = {
        "assigned": "awaiting send",
        "used": "verified",
        "expired": "expired",
        "used_no_match": "sent (no OTP match)",
    }
    history_lines = []
    for otp_value, tick_size, status, assigned_at, used_at, tx_signature in rows[:10]:
        label = status_labels.get(status, status or "unknown")
        ts = _discord_time_from_sqlite(used_at or assigned_at)
        sig_part = f" | tx {tx_signature[:12]}…" if tx_signature else ""
        history_lines.append(f"- `{otp_value}` | {label} | {ts}{sig_part}")

    if history_lines:
        await interaction.followup.send(
            "**Verification history:**\n" + "\n".join(history_lines),
            ephemeral=True,
        )

    if not _is_leaderboard_visible(discord_id):
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


@bot.command(name="setdonationleaderboard")
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
        "Send FARTBOY, SOL, USDT or USDC to the following SOLANA address to donate:"
    )
    await channel.send(war_chest)
    info = (
        "**How to verify your wallet:**\n"
        "1. Donate at least **1% of your FARTBOY snapshot balance** to the war chest.\n"
        "2. Click **Verify wallet** to get a unique OTP amount.\n"
        "3. Send that exact OTP amount of FARTBOY to the war chest.\n"
        "4. Click **Verify status** to confirm — done!\n\n"
        "Choosing to appear on the leaderboard is optional and does not affect perks.\n"
        "If something doesn't work or you need help, ask the mods."
    )
    await channel.send(info, view=VerificationButtonsView())


@bot.command(name="setdonationleadersize")
async def setdonationleadersize(ctx: commands.Context, limit: int = DEFAULT_LIMIT):
    limit = max(1, min(limit, 100))
    _set_limit("donations", limit)
    await ctx.send(f"Donation leaderboard size set to {limit}.")


@bot.command(name="settrackerinterval")
async def settrackerinterval(ctx: commands.Context, seconds: int = 0):
    if seconds <= 0:
        return await ctx.send("Usage: `!settrackerinterval SECONDS`")
    seconds = max(5, min(seconds, 3600))
    bot.track_incoming.change_interval(seconds=seconds)
    await ctx.send(f"Tracker interval set to {seconds} seconds.")


@bot.command(name="setpagelimit")
async def setpagelimit(ctx: commands.Context, limit: int = 0):
    if limit <= 0:
        return await ctx.send("Usage: `!setpagelimit LIMIT`")
    limit = max(1, min(limit, 1000))
    if not bot._tracker:
        return await ctx.send("Tracker is not configured (missing env vars).")
    bot._tracker.page_limit = limit
    await ctx.send(f"Tracker page limit set to {limit} signatures per address.")


@bot.command(name="setlogchannel")
async def setlogchannel(ctx: commands.Context, channel: discord.TextChannel | None = None):
    if channel is None:
        current = _get_donor_config("otp_log_channel_id")
        if current:
            return await ctx.send(f"OTP log channel: <#{current}>")
        return await ctx.send("No OTP log channel set. Usage: `!setlogchannel #channel`")
    _set_donor_config("otp_log_channel_id", str(channel.id))
    await ctx.send(f"OTP log channel set to {channel.mention}.")


@bot.command(name="golive")
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
        "details": (
            "Exchange wallets cannot be OTP-verified or !manualverify'd; use "
            "`!addtransaction SIG @user` to attribute a specific donation. "
            "They stay excluded from the public wallet leaderboard column."
        ),
    },
    "removeexchangewallets": {
        "summary": "Remove wallets from the exchange list.",
        "usage": "`!removeexchangewallets WALLET1 WALLET2 ...`",
        "details": "Does not restore any previous verification links.",
    },
    "manualverify": {
        "summary": "Manually link a wallet to a Discord user.",
        "usage": "`!manualverify @user WALLET_ADDRESS`",
        "details": (
            "Links the wallet to the user. If the wallet is not in the snapshot, "
            "creates a new row with zero balance. Off-snapshot wallets need "
            "`!setuserthreshold @user true` for perk eligibility. "
            "If the wallet is already linked to another user, the command refuses; "
            "use `!removeverification WALLET_ADDRESS` first to unlink."
        ),
    },
    "removeverification": {
        "summary": "Remove verification for a user or wallet.",
        "usage": "`!removeverification WALLET_ADDRESS`",
        "details": "Dangerous: unlinks that wallet and clears any donation links.",
    },
    "setvisibility": {
        "summary": "Show or hide a user on the public leaderboard.",
        "usage": "`!setvisibility @user visible|hidden`",
        "details": (
            "Sets leaderboard visibility for the given user. 'visible' shows their name; "
            "'hidden' makes them anonymous. The user must have a verified wallet."
        ),
    },
    "setuserthreshold": {
        "summary": "Override perk eligibility threshold for a user.",
        "usage": "`!setuserthreshold @user true|false|reset`",
        "details": (
            "true = force eligible, false = force ineligible, reset = use automatic 1% check. "
            "This is the only command that controls threshold overrides."
        ),
    },
    "setverificationorder": {
        "summary": "Set OTP verification mode (strict or flexible).",
        "usage": "`!setverificationorder strict|flexible`",
        "details": (
            "strict (default): OTP only accepted after the wallet has donated >= 1% of snapshot holdings. "
            "flexible: OTP links wallet without requiring 1% donation first. "
            "No args prints current setting."
        ),
    },
    "synccommands": {
        "summary": "Sync slash commands.",
        "usage": "`!synccommands`",
        "details": "Useful after adding or changing slash commands.",
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
        "details": (
            "Optional @user sets discord_id on each incoming leg of that signature in the tx DB "
            "(including sends from exchange-marked wallets). This attributes the donation to the "
            "user for summaries and lookups; it does not verify an exchange wallet to that user "
            "(snapshot discord_id / OTP are unchanged for exchange senders)."
        ),
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
    "setbaserole": {
        "summary": "Set the base contributor role for qualifying donors.",
        "usage": "`!setbaserole <role_name>`",
        "details": (
            "Every donor who qualifies for any tier receives this role.\n"
            "Example: `!setbaserole Contributor`\n"
            "The role is created automatically if it doesn't exist."
        ),
    },
    "settier": {
        "summary": "Add a donation tier with emoji and role.",
        "usage": "`!settier <min_usd> <emoji> <role_name>`",
        "details": (
            "Creates a tier at the given USD threshold. Donors at or above this amount "
            "get the emoji as a nickname prefix and the role assigned.\n"
            "Examples:\n"
            "  `!settier 50 \U0001f949 Bronze Donor`\n"
            "  `!settier 100 \U0001f948 Silver Donor`\n"
            "  `!settier 500 \U0001f451 Gold Donor`\n"
            "Only the highest qualifying tier's emoji and role are applied."
        ),
    },
    "removetier": {
        "summary": "Remove a donation tier by ID.",
        "usage": "`!removetier <id>`",
        "details": "Use `!tiers` to see tier IDs.",
    },
    "tiers": {
        "summary": "Show all active donation tiers and the base role.",
        "usage": "`!tiers`",
        "details": (
            "Displays the base role and all tier thresholds with their emoji and role name."
        ),
    },
    "syncdonorroles": {
        "summary": "Force a full donor role and nickname sync.",
        "usage": "`!syncdonorroles`",
        "details": (
            "Recomputes verified_users donation totals from snapshot and transaction data, "
            "then re-evaluates every verified user against the configured tiers and updates "
            "their Discord roles and nickname emoji prefix. Normally this runs automatically "
            "after each tracker cycle, but this command triggers it manually."
        ),
    },
    "recomputesummaries": {
        "summary": "Rebuild verified_users donation totals for all linked members.",
        "usage": "`!recomputesummaries`",
        "details": (
            "Fixes stale leaderboard amounts and tier sync when snapshot donated_usd "
            "and verified_users.total_donated_usd have diverged. Refreshes leaderboard embeds."
        ),
    },
    "donorstats": {
        "summary": "Show donation statistics.",
        "usage": "`!donorstats [min_usd]`",
        "details": "Shows totals, averages, and counts. Optional min_usd filters donors above that amount.",
    },
    "walletsummary": {
        "summary": "Show wallet verification status and holdings breakdown.",
        "usage": "`!walletsummary`",
        "details": "Displays counts of verified vs unverified wallets and aggregate holdings.",
    },
    "donortop": {
        "summary": "Show top donors by USD donated.",
        "usage": "`!donortop [count]`",
        "details": "Defaults to top 10. Max 25.",
    },
    "debugroles": {
        "summary": "Dump guild roles and stored tier config for diagnosis.",
        "usage": "`!debugroles`",
        "details": "Shows all server roles and the bot's stored base role / tier IDs.",
    },
    "setlogchannel": {
        "summary": "Set the admin OTP log channel.",
        "usage": "`!setlogchannel #channel`",
        "details": "Every OTP assignment will be logged here with username and amount. No args shows current.",
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
            "setuserthreshold",
            "setvisibility",
            "setverificationorder",
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
        "Donor Tiers": [
            "setbaserole",
            "settier",
            "removetier",
            "tiers",
            "recomputesummaries",
            "syncdonorroles",
        ],
        "Stats": [
            "donorstats",
            "walletsummary",
            "donortop",
        ],
        "Admin utilities": [
            "settrackerinterval",
            "setpagelimit",
            "setdonationleadersize",
            "setlogchannel",
            "debugroles",
            "synccommands",
            "listcommands",
            "help",
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
            "3) `!golive`\n\n"
            "**Optional — donor tiers (after go-live):**\n"
            "4) `!setbaserole Contributor`\n"
            "5) `!settier 50 \U0001f949 Bronze Donor` (repeat for each tier)\n"
            "6) `!syncdonorroles` (force first sync)"
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
        n = recompute_all_verified_summaries(
            snapshot_db=SNAPSHOT_DB,
            snapshot_table=SNAPSHOT_TABLE,
            tx_db_path=TX_DB,
            tx_table=TX_TABLE,
        )
        await bot._refresh_leaderboards()
        await ctx.send(
            f"Snapshot completed. Recomputed donation summaries for **{n}** verified user(s)."
        )
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
    discord_id = str(member.id)
    discord_name = str(member)
    existing_holder = _get_snapshot_discord_id_for_wallet(wallet)
    if existing_holder is not None and existing_holder != discord_id:
        return await ctx.send(
            f"This wallet is already verified and linked to <@{existing_holder}>. "
            f"To assign it to someone else, run `!removeverification {wallet}` first."
        )
    inserted_new_row = False
    success = _update_wallet_verification(
        wallet=wallet,
        discord_id=discord_id,
        discord_name=discord_name,
        on_leaderboard=on_leaderboard,
    )
    if not success:
        try:
            with _connect_db(SNAPSHOT_DB) as conn:
                conn.execute(
                    f"""
                    INSERT INTO {SNAPSHOT_TABLE}
                        (wallet_address, amount_fartboy, donated_fartboy, donated_usd,
                         discord_id, discord_name, on_leaderboard)
                    VALUES (?, 0, 0, 0, ?, ?, ?)
                    """,
                    (wallet, discord_id, discord_name, on_leaderboard),
                )
                conn.commit()
                inserted_new_row = True
        except sqlite3.Error as exc:
            log.error("Failed to insert snapshot row for manual verify: %s", exc)
            return await ctx.send("Failed to create snapshot row. Check logs.")
    _set_summary_visibility(discord_id, on_leaderboard)
    recompute_summary_for_discord_id(discord_id)
    try:
        with _connect_db(TX_DB) as tx_conn:
            tx_conn.execute(
                f"""
                UPDATE {TX_TABLE}
                SET discord_id = ?
                WHERE sender_wallet = ?
                """,
                (discord_id, wallet),
            )
            tx_conn.commit()
    except sqlite3.Error as exc:
        log.warning("Failed to update tx discord id for manual verify: %s", exc)
    msg = f"Linked `{wallet}` to {member.mention} (leaderboard visibility: off)."
    if inserted_new_row:
        msg += (
            "\nWallet was not in the original snapshot (balance=0). "
            "To grant perk eligibility, also run `!setuserthreshold @user true`."
        )
    await ctx.send(msg)


@bot.command(name="removeverification")
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


@bot.command(name="setuserthreshold")
async def setuserthreshold(ctx: commands.Context, member: discord.Member | None = None, value: str = ""):
    if not member or value not in ("true", "false", "reset"):
        return await ctx.send("Usage: `!setuserthreshold @user true|false|reset`")
    discord_id = str(member.id)
    actor_id = str(ctx.author.id)
    try:
        with _connect_db(STATE_DB) as conn:
            _ensure_user_threshold_resolution_schema()
            if value == "reset":
                conn.execute(
                    "DELETE FROM user_threshold_resolution WHERE discord_id = ?",
                    (discord_id,),
                )
                conn.commit()
                return await ctx.send(
                    f"Threshold override removed for {member.mention}. Automatic eligibility applies."
                )
            resolution = "force_met" if value == "true" else "force_not_met"
            conn.execute(
                """
                INSERT INTO user_threshold_resolution (discord_id, resolution, updated_at, actor_discord_id)
                VALUES (?, ?, datetime('now'), ?)
                ON CONFLICT(discord_id) DO UPDATE SET
                    resolution = excluded.resolution,
                    updated_at = excluded.updated_at,
                    actor_discord_id = excluded.actor_discord_id
                """,
                (discord_id, resolution, actor_id),
            )
            conn.commit()
        label = "met (force)" if value == "true" else "not met (force)"
        await ctx.send(f"Threshold for {member.mention} set to **{label}**.")
    except sqlite3.Error as exc:
        log.error("Failed to set user threshold: %s", exc)
        await ctx.send("Failed to update threshold. Check logs.")


@bot.command(name="setvisibility")
@commands.has_permissions(manage_guild=True)
async def setvisibility(ctx: commands.Context, member: discord.Member | None = None, value: str = ""):
    """Admin command to show or hide a user on the public leaderboard."""
    if not member or value not in ("visible", "hidden"):
        return await ctx.send("Usage: `!setvisibility @user visible|hidden`")
    discord_id = str(member.id)
    discord_name = str(member)
    visible = 1 if value == "visible" else 0
    ok = _update_visibility_by_discord(discord_id, discord_name, visible)
    if not ok:
        return await ctx.send(
            f"No verified wallet found for {member.mention}. They need to verify first."
        )
    _set_summary_visibility(discord_id, visible)
    await bot._refresh_leaderboards()
    label = "visible" if visible else "hidden"
    await ctx.send(f"Leaderboard visibility for {member.mention} set to **{label}**.")


@bot.command(name="setverificationorder")
async def setverificationorder(ctx: commands.Context, mode: str = ""):
    _ensure_donor_config_schema()
    if not mode:
        current = _get_donor_config("verification_order") or "strict"
        return await ctx.send(f"Current verification order: **{current}**")
    if mode not in ("strict", "flexible"):
        return await ctx.send("Usage: `!setverificationorder strict|flexible`")
    _set_donor_config("verification_order", mode)
    await ctx.send(f"Verification order set to **{mode}**.")


@bot.command(name="setexchangewallets")
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
            recompute_summary_for_discord_id(discord_id)
        await ctx.send(
            f"Recorded {len(cleaned)} exchange wallet(s) for `{exchange_name}`."
        )
        await bot._refresh_leaderboards()
    except sqlite3.Error as exc:
        log.exception("Failed to set exchange wallets: %s", exc)
        await ctx.send("Failed to store exchange wallets. Check logs.")


@bot.command(name="removeexchangewallets")
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
async def synccommands(ctx: commands.Context):
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            await bot.tree.sync(guild=guild)
            await ctx.send("Synced guild slash commands.")
        else:
            await bot.tree.sync()
            await ctx.send("Synced global slash commands.")
    except discord.DiscordException as exc:
        log.exception("Failed to sync commands: %s", exc)
        await ctx.send("Failed to sync slash commands. Check logs.")


@bot.command(name="debugroles")
async def debugroles(ctx: commands.Context):
    """Dump all guild roles and the bot's stored config for diagnosis."""
    if not ctx.guild:
        return await ctx.send("Must be run in a guild.")
    lines = ["**All guild roles (name → id):**"]
    for r in sorted(ctx.guild.roles, key=lambda r: r.position, reverse=True):
        if r.name == "@everyone":
            continue
        lines.append(f"`{r.name}` → {r.id}")
    lines.append("")
    base_name = _get_donor_config("base_role_name") or "(not set)"
    base_id = _get_donor_config("base_role_id") or "(not set)"
    lines.append(f"**Stored base_role_name:** `{base_name}`")
    lines.append(f"**Stored base_role_id:** `{base_id}`")
    tiers = _fetch_tiers()
    if tiers:
        lines.append("\n**Stored tiers:**")
        for t in tiers:
            rid = t.get("role_id") or "(none)"
            lines.append(f"  #{t['id']}: ${t['min_usd']:,.2f} — {t['emoji']} role_name=`{t['role_name']}` role_id=`{rid}`")
    else:
        lines.append("\n**No tiers configured.**")
    msg = "\n".join(lines)
    if len(msg) > 1900:
        for i in range(0, len(msg), 1900):
            await ctx.send(msg[i:i+1900])
    else:
        await ctx.send(msg)


@bot.command(name="listcommands")
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
async def addtransaction(ctx: commands.Context, signature: str = ""):
    if not signature:
        return await ctx.send("Usage: `!addtransaction SIGNATURE [@user]`")
    discord_id = str(ctx.message.mentions[0].id) if ctx.message.mentions else None
    await ctx.send("Processing transaction...")
    total, inserted = await _process_signature(signature.strip(), discord_id=discord_id)
    if total == 0:
        return await ctx.send("No incoming transfers for the war chest found in that signature.")
    if inserted == 0 and not discord_id:
        return await ctx.send("Transaction already recorded or pricing unavailable.")
    await bot._refresh_leaderboards()
    if inserted > 0:
        await ctx.send(f"Recorded {inserted} incoming transfer(s) from that signature.")
    else:
        await ctx.send(
            "Transaction was already in the database; Discord attribution on those rows was updated."
        )


@setdonationleaderboard.error
@setdonationleadersize.error
async def setleaderboard_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need **Manage Server** to run this.")
    else:
        log.exception("Failed to set leaderboard: %s", error)
        await ctx.send("Failed to set leaderboard. Check logs.")


@bot.command(name="settarget")
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


@bot.command(name="setbaserole")
async def setbaserole(ctx: commands.Context, *, role_name: str = ""):
    """Set the base contributor role given to all qualifying donors."""
    if not role_name.strip():
        return await ctx.send("Usage: `!setbaserole <role_name>`\nExample: `!setbaserole Contributor`")
    role = _lookup_guild_role_by_name(ctx.guild, role_name.strip())
    if not role:
        mention_match = re.match(r"<@&(\d+)>$", role_name.strip())
        if mention_match:
            role = ctx.guild.get_role(int(mention_match.group(1)))
    if not role:
        return await ctx.send("Could not find that role in this server.")
    _set_donor_config("base_role_name", role.name)
    _set_donor_config("base_role_id", str(role.id))
    await ctx.send(f"Base donor role set to **{role.name}** (id: {role.id}).")


@bot.command(name="settier")
async def settier(ctx: commands.Context, min_usd: str = "", emoji: str = "", *, role_name: str = ""):
    """Add a donation tier. Usage: !settier <min_usd> <emoji> <role_name>"""
    if not min_usd or not emoji or not role_name.strip():
        return await ctx.send(
            "Usage: `!settier <min_usd> <emoji> <role_name>`\n"
            "Example: `!settier 100 \U0001f396 Silver Donor`"
        )
    try:
        amount = float(min_usd.replace(",", "").replace("$", ""))
        if amount < 0:
            return await ctx.send("Minimum USD must be zero or positive.")
    except ValueError:
        return await ctx.send("Invalid amount. Use a number like `100` or `250.00`.")
    role = _lookup_guild_role_by_name(ctx.guild, role_name.strip())
    if not role:
        mention_match = re.match(r"<@&(\d+)>$", role_name.strip())
        if mention_match:
            role = ctx.guild.get_role(int(mention_match.group(1)))
    if not role:
        return await ctx.send("Could not find that role in this server.")
    tier_id = _add_tier(amount, emoji.strip(), role.name, role_id=str(role.id))
    if tier_id is None:
        return await ctx.send("Failed to add tier. Check logs.")
    await ctx.send(
        f"Tier #{tier_id} added: **${amount:,.2f}** — {emoji.strip()} **{role.name}** (id: {role.id})"
    )


@bot.command(name="removetier")
async def removetier(ctx: commands.Context, tier_id: str = ""):
    """Remove a donation tier by ID. Usage: !removetier <id>"""
    if not tier_id:
        return await ctx.send("Usage: `!removetier <id>`\nUse `!tiers` to see all tiers and their IDs.")
    try:
        tid = int(tier_id)
    except ValueError:
        return await ctx.send("Tier ID must be a number.")
    success = _remove_tier(tid)
    if success:
        await ctx.send(f"Tier #{tid} removed.")
    else:
        await ctx.send(f"Tier #{tid} not found or already removed.")


@bot.command(name="tiers")
async def tiers_cmd(ctx: commands.Context):
    """Show all active donation tiers."""
    tiers = _fetch_tiers()
    base_role = _get_donor_config("base_role_name")

    lines = []
    if base_role:
        lines.append(f"**Base role:** {base_role} (given to all qualifying donors)")
    else:
        lines.append("**Base role:** Not set. Use `!setbaserole <name>` to configure.")

    if not tiers:
        lines.append("\nNo donation tiers configured. Use `!settier <min_usd> <emoji> <role_name>` to add one.")
    else:
        lines.append("\n**Tiers** (ascending by minimum donation):")
        for t in tiers:
            lines.append(
                f"#{t['id']}: **${t['min_usd']:,.2f}** — {t['emoji']} **{t['role_name']}**"
            )
        lines.append("\nTier assignment is based on total donated USD meeting the tier minimum.")

    await ctx.send("\n".join(lines))


@bot.command(name="recomputesummaries")
async def recomputesummaries(ctx: commands.Context):
    """Rebuild verified_users totals from snapshot + tx data."""
    await ctx.send("Recomputing donation summaries...")
    try:
        n = recompute_all_verified_summaries(
            snapshot_db=SNAPSHOT_DB,
            snapshot_table=SNAPSHOT_TABLE,
            tx_db_path=TX_DB,
            tx_table=TX_TABLE,
        )
        await bot._refresh_leaderboards()
        await ctx.send(
            f"Recomputed donation summaries for **{n}** verified user(s) and refreshed leaderboards."
        )
    except sqlite3.Error as exc:
        log.exception("recomputesummaries failed: %s", exc)
        await ctx.send("Failed to recompute summaries. Check logs.")


@bot.command(name="syncdonorroles")
async def syncdonorroles(ctx: commands.Context):
    """Force a full donor role and nickname sync for all verified users."""
    if not GUILD_ID:
        return await ctx.send("DISCORD_GUILD_ID not configured.")
    guild = bot.get_guild(int(GUILD_ID))
    if not guild:
        return await ctx.send("Could not find the configured guild.")
    await ctx.send("Starting donor role sync...")
    try:
        n = recompute_all_verified_summaries(
            snapshot_db=SNAPSHOT_DB,
            snapshot_table=SNAPSHOT_TABLE,
            tx_db_path=TX_DB,
            tx_table=TX_TABLE,
        )
        log.info("Recomputed %s verified user summaries before role sync.", n)
        counts = await sync_donor_roles(guild)
        lines = ["**Donor role sync complete.**", ""]
        if counts["debug"]:
            for d in counts["debug"]:
                lines.append(d)
            lines.append("")
        summary_parts = [f"{counts['processed']} processed"]
        if counts["updated"]:
            summary_parts.append(f"{counts['updated']} updated")
        if counts["skipped_base"]:
            summary_parts.append(f"{counts['skipped_base']} skipped (1% not met)")
        if counts["skipped_tier"]:
            summary_parts.append(f"{counts['skipped_tier']} skipped (no tier match)")
        if counts["failed_permission"]:
            summary_parts.append(f"{counts['failed_permission']} failed (permission)")
        if counts["skipped_nick"]:
            summary_parts.append(f"{counts['skipped_nick']} nickname skipped")
        lines.append(" | ".join(summary_parts))
        msg = "\n".join(lines)
        for chunk in [msg[i:i+1900] for i in range(0, len(msg), 1900)]:
            await ctx.send(chunk)
        await bot._refresh_leaderboards()
    except Exception as exc:
        log.exception("Manual donor role sync failed: %s", exc)
        await ctx.send(f"Sync failed: {exc}")


@bot.command(name="donorstats")
async def donorstats(ctx: commands.Context, min_usd: str = ""):
    """Show donation statistics. Optionally filter by minimum USD: !donorstats <min_usd>"""
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            total_verified = conn.execute(
                f"SELECT COUNT(*) FROM {SUMMARY_TABLE} WHERE discord_id IS NOT NULL"
            ).fetchone()[0]

            total_wallets = conn.execute(
                f"SELECT COUNT(*) FROM {SNAPSHOT_TABLE}"
            ).fetchone()[0]

            verified_wallets = conn.execute(
                f"SELECT COUNT(*) FROM {SNAPSHOT_TABLE} WHERE discord_id IS NOT NULL"
            ).fetchone()[0]

            unverified_wallets = total_wallets - verified_wallets

            donors = conn.execute(
                f"SELECT COUNT(*) FROM {SUMMARY_TABLE} WHERE discord_id IS NOT NULL AND total_donated_usd > 0"
            ).fetchone()[0]

            total_donated = conn.execute(
                f"SELECT COALESCE(SUM(total_donated_usd), 0) FROM {SUMMARY_TABLE} WHERE discord_id IS NOT NULL"
            ).fetchone()[0]

            eligible = conn.execute(
                f"""SELECT COUNT(DISTINCT s.discord_id)
                    FROM {SNAPSHOT_TABLE} s
                    WHERE s.discord_id IS NOT NULL
                      AND s.amount_fartboy > 0
                      AND s.donated_fartboy >= s.amount_fartboy * 0.01"""
            ).fetchone()[0]

            lines = [
                "**Donation & Verification Stats**",
                f"Verified members: **{total_verified}**",
                f"Verified wallets: **{verified_wallets}**",
                f"Unverified wallets: **{unverified_wallets}**",
                f"Members who donated: **{donors}**",
                f"Members meeting 1% threshold: **{eligible}**",
                f"Total donated (USD): **${total_donated:,.2f}**",
            ]

            if min_usd.strip():
                try:
                    threshold = float(min_usd)
                    above = conn.execute(
                        f"SELECT COUNT(*) FROM {SUMMARY_TABLE} WHERE discord_id IS NOT NULL AND total_donated_usd >= ?",
                        (threshold,),
                    ).fetchone()[0]
                    lines.append(f"Members donated >= ${threshold:,.2f}: **{above}**")
                except ValueError:
                    lines.append(f"(Invalid threshold `{min_usd}` — provide a number)")

        await ctx.send("\n".join(lines))
    except sqlite3.Error as exc:
        log.error("donorstats failed: %s", exc)
        await ctx.send("Failed to fetch stats. Check logs.")


@bot.command(name="walletsummary")
async def walletsummary(ctx: commands.Context):
    """Show a breakdown of wallet verification status and holdings."""
    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            total_wallets = conn.execute(
                f"SELECT COUNT(*) FROM {SNAPSHOT_TABLE}"
            ).fetchone()[0]

            verified_wallets = conn.execute(
                f"SELECT COUNT(*) FROM {SNAPSHOT_TABLE} WHERE discord_id IS NOT NULL"
            ).fetchone()[0]

            total_holdings = conn.execute(
                f"SELECT COALESCE(SUM(amount_fartboy), 0) FROM {SNAPSHOT_TABLE}"
            ).fetchone()[0]

            verified_holdings = conn.execute(
                f"SELECT COALESCE(SUM(amount_fartboy), 0) FROM {SNAPSHOT_TABLE} WHERE discord_id IS NOT NULL"
            ).fetchone()[0]

            total_donated_tokens = conn.execute(
                f"SELECT COALESCE(SUM(donated_fartboy), 0) FROM {SNAPSHOT_TABLE}"
            ).fetchone()[0]

            pct_wallets = (verified_wallets / total_wallets * 100) if total_wallets else 0
            pct_holdings = (verified_holdings / total_holdings * 100) if total_holdings else 0

            lines = [
                "**Wallet Summary**",
                f"Total wallets in snapshot: **{total_wallets:,}**",
                f"Verified: **{verified_wallets:,}** ({pct_wallets:.1f}%)",
                f"Unverified: **{total_wallets - verified_wallets:,}**",
                "",
                f"Total holdings: **{total_holdings:,.0f}** FARTBOY",
                f"Verified holdings: **{verified_holdings:,.0f}** ({pct_holdings:.1f}%)",
                f"Total donated (tokens): **{total_donated_tokens:,.0f}** FARTBOY",
            ]

        await ctx.send("\n".join(lines))
    except sqlite3.Error as exc:
        log.error("walletsummary failed: %s", exc)
        await ctx.send("Failed to fetch wallet summary. Check logs.")


@bot.command(name="donortop")
async def donortop(ctx: commands.Context, count: str = "10"):
    """Show top donors by USD donated. Usage: !donortop [count]"""
    try:
        limit = min(max(int(count), 1), 25)
    except ValueError:
        limit = 10

    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            rows = conn.execute(
                f"""SELECT discord_name, total_donated_usd
                    FROM {SUMMARY_TABLE}
                    WHERE discord_id IS NOT NULL AND total_donated_usd > 0
                    ORDER BY total_donated_usd DESC
                    LIMIT ?""",
                (limit,),
            ).fetchall()

        if not rows:
            return await ctx.send("No donations recorded yet.")

        lines = [f"**Top {len(rows)} Donors (by USD)**"]
        for i, (name, usd) in enumerate(rows, 1):
            display = name or "Anonymous"
            lines.append(f"{i}. {display} — **${float(usd):,.2f}**")

        await ctx.send("\n".join(lines))
    except sqlite3.Error as exc:
        log.error("donortop failed: %s", exc)
        await ctx.send("Failed to fetch top donors. Check logs.")


@setbaserole.error
@settier.error
@removetier.error
@syncdonorroles.error
@setvisibility.error
async def tier_cmd_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need **Manage Server** to run this.")
    else:
        log.exception("Tier command error: %s", error)
        await ctx.send("Failed to execute tier command. Check logs.")


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
