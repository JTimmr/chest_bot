#!/usr/bin/env python3
"""
Discord bot: maintains a permanent Top 30 holders message.

Commands:
  !setholderleaderboard #channel  -> post the message and keep updating it.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import List, Tuple, Set

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from incoming_tracker import (
    IncomingTracker,
    HeliusRPCClient,
    USDC_MINT,
    SOL_DECIMALS,
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
)

load_dotenv()

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
TRACKER_INTERVAL_SECONDS = int(os.getenv("TRACKER_INTERVAL_SECONDS", "30"))
TX_DB = os.getenv("TX_DB", "/app/data/incoming_transactions.db")
TX_TABLE = os.getenv("TX_TABLE", "incoming_transactions")
TX_LOOKUP_LIMIT = int(os.getenv("TX_LOOKUP_LIMIT", "50"))
RPC_REQUEST_DELAY = float(os.getenv("RPC_REQUEST_DELAY", "0.1"))
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
                ('holders', ?),
                ('donations', ?)
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


def _fetch_top_holders(limit: int = DEFAULT_LIMIT) -> List[Tuple[str, float, str | None, str | None, int]]:
    try:
        cols = _get_snapshot_columns()
        with _connect_db(SNAPSHOT_DB) as conn:
            select_cols = ["wallet_address", "amount_fartboy"]
            select_cols.append("discord_id" if "discord_id" in cols else "NULL AS discord_id")
            select_cols.append("discord_name" if "discord_name" in cols else "NULL AS discord_name")
            select_cols.append("on_leaderboard" if "on_leaderboard" in cols else "0 AS on_leaderboard")
            rows = conn.execute(
                f"""
                SELECT {", ".join(select_cols)}
                FROM {SNAPSHOT_TABLE}
                ORDER BY amount_fartboy DESC,
                         on_leaderboard DESC,
                         CASE WHEN discord_id IS NULL THEN 0 ELSE 1 END DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [(r[0], float(r[1]), r[2], r[3], int(r[4] or 0)) for r in rows]
    except sqlite3.Error as exc:
        log.error("Failed to read snapshot DB %s:%s - %s", SNAPSHOT_DB, SNAPSHOT_TABLE, exc)
        return []


def _format_wallet(addr: str) -> str:
    return addr[:8] if addr else "UNKNOWN"


def _render_holders_embed(limit: int) -> discord.Embed:
    holders = _fetch_top_holders(limit)
    emb = discord.Embed(
        title=f"Top {limit} FARTBOY Holders",
        color=0x00D18F,
        timestamp=datetime.now(timezone.utc),
    )
    if not holders:
        emb.add_field(name="No data", value="No holders found in snapshot database.", inline=False)
        emb.set_footer(text="Updated")
        return emb

    lines = []
    for idx, (wallet, amount, discord_id, discord_name, on_leaderboard) in enumerate(holders, start=1):
        display = _format_wallet(wallet)
        if on_leaderboard and (discord_name or discord_id):
            display = discord_name or f"<@{discord_id}>"
        lines.append(f"{idx:>2}. {display} — {amount:,.4f}")
    emb.add_field(name="Leaderboard", value="\n".join(lines), inline=False)
    emb.set_footer(text="Updated")
    return emb


def _fetch_top_donors(limit: int = DEFAULT_LIMIT) -> List[Tuple[str, float, str | None, str | None, int]]:
    try:
        cols = _get_snapshot_columns()
        with _connect_db(SNAPSHOT_DB) as conn:
            select_cols = ["wallet_address", "donated_usd"]
            select_cols.append("discord_id" if "discord_id" in cols else "NULL AS discord_id")
            select_cols.append("discord_name" if "discord_name" in cols else "NULL AS discord_name")
            select_cols.append("on_leaderboard" if "on_leaderboard" in cols else "0 AS on_leaderboard")
            rows = conn.execute(
                f"""
                SELECT {", ".join(select_cols)}
                FROM {SNAPSHOT_TABLE}
                """,
            ).fetchall()
        aggregated: dict[str, dict] = {}
        singles: List[Tuple[str, float, str | None, str | None, int]] = []
        for wallet, donated_usd, discord_id, discord_name, on_leaderboard in rows:
            donated_usd = float(donated_usd or 0)
            on_leaderboard = int(on_leaderboard or 0)
            if discord_id and on_leaderboard:
                key = str(discord_id)
                entry = aggregated.get(key)
                if not entry:
                    aggregated[key] = {
                        "wallet": wallet,
                        "donated_usd": donated_usd,
                        "discord_id": discord_id,
                        "discord_name": discord_name,
                        "on_leaderboard": on_leaderboard,
                    }
                else:
                    entry["donated_usd"] += donated_usd
                    if not entry.get("discord_name") and discord_name:
                        entry["discord_name"] = discord_name
            else:
                singles.append((wallet, donated_usd, discord_id, discord_name, on_leaderboard))

        combined: List[Tuple[str, float, str | None, str | None, int]] = []
        for entry in aggregated.values():
            combined.append(
                (
                    entry["wallet"],
                    float(entry["donated_usd"]),
                    entry["discord_id"],
                    entry["discord_name"],
                    int(entry["on_leaderboard"]),
                )
            )
        combined.extend(singles)

        combined.sort(
            key=lambda r: (r[1], r[4], 1 if r[2] else 0),
            reverse=True,
        )
        return combined[:limit]
    except sqlite3.Error as exc:
        log.error("Failed to read snapshot DB %s:%s - %s", SNAPSHOT_DB, SNAPSHOT_TABLE, exc)
        return []


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


def _fetch_transactions_for_wallet(wallet: str, limit: int) -> List[dict]:
    try:
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


async def _process_signature(signature: str) -> Tuple[int, int]:
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

        allowed_spl_mints = {fartboy_mint, USDC_MINT}
        for inst in _iter_token_transfer_instructions(tx):
            parsed = _parse_transfer(inst, token_map)
            if not parsed:
                continue
            source, destination, amount_raw, decimals, mint = parsed
            destination_owner = token_map.get(destination, {}).get("owner")
            source_owner = token_map.get(source, {}).get("owner")
            if not destination_owner or not source_owner:
                continue
            if destination_owner != target_wallet:
                continue
            if mint not in allowed_spl_mints:
                continue
            rows.append(
                {
                    "signature": signature,
                    "timestamp": tx.get("blockTime"),
                    "amount_raw": amount_raw,
                    "amount_ui": _ui_amount(amount_raw, decimals),
                    "token": "FARTBOY" if mint == fartboy_mint else "USDC",
                    "sender_wallet": source_owner,
                }
            )

    if not rows:
        return 0, 0

    computed = await _compute_values_for_rows(rows, fartboy_mint)
    if not computed:
        return len(rows), 0

    inserted = 0
    with sqlite3.connect(TX_DB) as tx_conn, sqlite3.connect(SNAPSHOT_DB) as snapshot_conn:
        _init_transactions_db(tx_conn, TX_TABLE)
        _init_snapshot_db(snapshot_conn, SNAPSHOT_TABLE)
        for row, value_usdc, value_fartboy in computed:
            cur = tx_conn.cursor()
            cur.execute(
                f"""
                INSERT OR IGNORE INTO {TX_TABLE} (
                    signature, timestamp, sender_wallet, token, amount_raw,
                    amount_ui, value_usdc, value_fartboy
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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


def _render_donations_embed(limit: int) -> discord.Embed:
    donors = _fetch_top_donors(limit)
    emb = discord.Embed(
        title=f"Top {limit} Donations",
        color=0xFFD166,
        timestamp=datetime.now(timezone.utc),
    )
    if not donors:
        emb.add_field(name="No data", value="No donations recorded yet.", inline=False)
        emb.set_footer(text="Updated")
        return emb

    lines = []
    for idx, (wallet, donated_usd, discord_id, discord_name, on_leaderboard) in enumerate(donors, start=1):
        display = _format_wallet(wallet)
        if on_leaderboard and (discord_name or discord_id):
            display = discord_name or f"<@{discord_id}>"
        lines.append(f"{idx:>2}. {display} — ${donated_usd:,.2f}")
    emb.add_field(name="Leaderboard", value="\n".join(lines), inline=False)
    emb.set_footer(text="Updated")
    return emb


class LeaderboardBot(commands.Bot):
    def __init__(self, **kwargs):
        super().__init__(command_prefix="!", **kwargs)
        _init_state_db()
        self._last_snapshot_mtime = 0.0
        self._last_refresh_ts = 0.0
        self._last_render_hash: dict[str, int] = {"holders": 0, "donations": 0}
        self._tracker: IncomingTracker | None = None
        self._tracker_disabled_logged = False

    async def setup_hook(self) -> None:
        _ensure_snapshot_schema()
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
        if GUILD_ID:
            # Clear any global commands to avoid duplicates.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.clear_commands(guild=guild)
            self.tree.add_command(verify_wallet, guild=guild)
            self.tree.add_command(set_leaderboard_visibility, guild=guild)
            self.tree.add_command(check_wallet, guild=guild)
            self.tree.add_command(my_transactions, guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", GUILD_ID)
        else:
            self.tree.add_command(verify_wallet)
            self.tree.add_command(set_leaderboard_visibility)
            self.tree.add_command(check_wallet)
            self.tree.add_command(my_transactions)
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
        for leaderboard_type in ("holders", "donations"):
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
                if leaderboard_type == "holders":
                    embed = _render_holders_embed(display_limit)
                else:
                    embed = _render_donations_embed(display_limit)
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
        if not self._tracker:
            if not self._tracker_disabled_logged:
                log.warning(
                    "Tracker loop skipped (no tracker configured). Check env vars."
                )
                self._tracker_disabled_logged = True
            return
        try:
            count = await self._tracker.run_once()
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


intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True

bot = LeaderboardBot(intents=intents)


@app_commands.command(name="verify", description="Verify your wallet with the bot.")
@app_commands.describe(wallet="Your wallet address")
async def verify_wallet(
    interaction: discord.Interaction,
    wallet: str,
):
    wallet = wallet.strip()
    exists, amount = _lookup_wallet(wallet)
    if not exists:
        return await interaction.response.send_message(
            "Wallet not found in the snapshot database. "
            "Make sure it currently holds FARTBOY and run `!snapshotholders`.",
            ephemeral=True,
        )
    success = _update_wallet_verification(
        wallet=wallet.strip(),
        discord_id=str(interaction.user.id),
        discord_name=str(interaction.user),
        on_leaderboard=0,
    )
    if not success:
        return await interaction.response.send_message(
            "Verification failed. Try `!snapshotholders` and run /verify again.",
            ephemeral=True,
        )
    await interaction.response.send_message(
        f"Verified. Your wallet is now linked to your Discord user. "
        f"Snapshot balance: {amount:,.4f} FARTBOY. "
        f"Use /leaderboardvisibility later to show your name.",
        ephemeral=True,
    )
    await bot._refresh_leaderboards()


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
            "No verified wallet found for your Discord user. Run /verify first.",
            ephemeral=True,
        )
    await interaction.response.send_message(
        "Updated leaderboard visibility.",
        ephemeral=True,
    )
    await bot._refresh_leaderboards()


@app_commands.command(
    name="checkwallet",
    description="Check if a wallet exists in the current snapshot and see its balance.",
)
@app_commands.describe(wallet="Wallet address to check")
async def check_wallet(
    interaction: discord.Interaction,
    wallet: str,
):
    exists, amount = _lookup_wallet(wallet.strip())
    if not exists:
        return await interaction.response.send_message(
            "Wallet not found in the current snapshot.",
            ephemeral=True,
        )
    await interaction.response.send_message(
        f"Wallet found. Snapshot balance: {amount:,.4f} FARTBOY.",
        ephemeral=True,
    )


@app_commands.command(
    name="mytransactions",
    description="Show recent transactions for the wallet linked to your Discord user.",
)
async def my_transactions(interaction: discord.Interaction):
    wallets = _find_wallets_for_discord_id(str(interaction.user.id))
    if not wallets:
        return await interaction.response.send_message(
            "No verified wallet found. Run /verify first.",
            ephemeral=True,
        )
    wallet = wallets[0]
    rows = _fetch_transactions_for_wallet(wallet, TX_LOOKUP_LIMIT)
    if not rows:
        return await interaction.response.send_message(
            "No transactions found for your wallet.",
            ephemeral=True,
        )
    lines = []
    for row in rows[:50]:
        lines.append(
            f"{row['timestamp']} | {row['amount_ui']} {row['token']} | "
            f"${row['value_usdc']:.6f} | {row['value_fartboy']:.8f} FB"
        )
    body = "\n".join(lines)
    await interaction.response.send_message(
        f"Transactions for `{wallet}`:\n```\n{body}\n```",
        ephemeral=True,
    )


@bot.command(name="setholderleaderboard")
@commands.has_permissions(manage_guild=True)
async def setleaderboard(
    ctx: commands.Context, channel: discord.TextChannel | None = None, limit: int = DEFAULT_LIMIT
):
    if channel is None:
        return await ctx.send("Usage: `!setholderleaderboard #channel`")
    limit = max(1, min(limit, 100))
    msg = await channel.send(embed=_render_holders_embed(limit))
    _save_state("holders", channel.id, msg.id, limit)
    await ctx.send(f"Holder leaderboard message set in {channel.mention}.")


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


@bot.command(name="setholderleadersize")
@commands.has_permissions(manage_guild=True)
async def setholderleadersize(ctx: commands.Context, limit: int = DEFAULT_LIMIT):
    limit = max(1, min(limit, 100))
    _set_limit("holders", limit)
    await ctx.send(f"Holder leaderboard size set to {limit}.")


@bot.command(name="setdonationleadersize")
@commands.has_permissions(manage_guild=True)
async def setdonationleadersize(ctx: commands.Context, limit: int = DEFAULT_LIMIT):
    limit = max(1, min(limit, 100))
    _set_limit("donations", limit)
    await ctx.send(f"Donation leaderboard size set to {limit}.")


@bot.command(name="donationbothelp")
async def donationbothelp(ctx: commands.Context):
    embed = discord.Embed(
        title="Donation Bot Commands",
        description="Leaderboard setup and sizing commands.",
        color=0x4EA8DE,
    )
    embed.add_field(
        name="Set Leaderboards",
        value=(
            "`!setholderleaderboard #channel [limit]`\n"
            "`!setdonationleaderboard #channel [limit]`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Set Sizes",
        value=(
            "`!setholderleadersize [limit]`\n"
            "`!setdonationleadersize [limit]`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Snapshots",
        value="`!snapshotholders`\n`!resetverification`",
        inline=False,
    )
    embed.add_field(
        name="Transactions",
        value="`!tx @user` or `!tx WALLET_ADDRESS`\n`!addtransaction SIGNATURE`",
        inline=False,
    )
    embed.add_field(
        name="Notes",
        value="Default limit is 30; max 100.",
        inline=False,
    )
    await ctx.send(embed=embed)


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
            conn.commit()
        await ctx.send("Verification data reset.")
        await bot._refresh_leaderboards()
    except sqlite3.Error as exc:
        log.exception("Failed to reset verification: %s", exc)
        await ctx.send("Failed to reset verification. Check logs.")


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
        return await ctx.send("Usage: `!addtransaction SIGNATURE`")
    await ctx.send("Processing transaction...")
    total, inserted = await _process_signature(signature.strip())
    if total == 0:
        return await ctx.send("No incoming transfers for the war chest found in that signature.")
    if inserted == 0:
        return await ctx.send("Transaction already recorded or pricing unavailable.")
    await ctx.send(f"Recorded {inserted} incoming transfer(s) from that signature.")


@setleaderboard.error
@setdonationleaderboard.error
@setholderleadersize.error
@setdonationleadersize.error
async def setleaderboard_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need **Manage Server** to run this.")
    else:
        log.exception("Failed to set leaderboard: %s", error)
        await ctx.send("Failed to set leaderboard. Check logs.")


def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("ERROR: Missing DISCORD_BOT_TOKEN in environment.")
    bot.run(token)


if __name__ == "__main__":
    main()
