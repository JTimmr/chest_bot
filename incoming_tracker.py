#!/usr/bin/env python3
"""
Track incoming transfers (FARTBOY + USDC + USDT SPL, and SOL) into a single wallet.

Stores each transfer in a transactions database and updates a snapshot database
with donated FARTBOY and USD values by sender wallet.
"""

import argparse
import asyncio
import json
import os
import sqlite3
import time
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple, Set

import aiohttp
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

MAX_RETRIES = 6
RETRY_DELAY_BASE = 1.0
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SOL_MINT = "So11111111111111111111111111111111111111112"
SOL_DECIMALS = 9
OTP_TABLE = os.getenv("OTP_TABLE", "otp_registry")
OTP_EXPIRY_SECONDS = int(os.getenv("OTP_EXPIRY_SECONDS", "3600"))
SUMMARY_TABLE = os.getenv("SUMMARY_TABLE", "verified_users")


class RateLimiter:
    def __init__(self, delay: float):
        self.delay = delay
        self.last_request = 0.0
        self.lock = asyncio.Lock()
        self.min_delay = max(0.05, delay)
        self.max_delay = 2.0

    async def wait(self) -> None:
        async with self.lock:
            elapsed = time.time() - self.last_request
            if elapsed < self.delay:
                await asyncio.sleep(self.delay - elapsed)
            self.last_request = time.time()

    async def on_rate_limited(self) -> None:
        async with self.lock:
            self.delay = min(self.max_delay, max(self.min_delay, self.delay * 1.35))


class HeliusRPCClient:
    def __init__(self, api_key: str, request_delay: float):
        self.api_key = api_key
        self.base_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
        self.rate_limiter = RateLimiter(request_delay)
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _make_request(self, method: str, params: List) -> Dict:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}

        for attempt in range(MAX_RETRIES):
            await self.rate_limiter.wait()
            try:
                async with self.session.post(self.base_url, json=payload) as resp:
                    if resp.status == 429:
                        await self.rate_limiter.on_rate_limited()
                        wait_time = RETRY_DELAY_BASE * (2 ** min(attempt, 4))
                        await asyncio.sleep(wait_time)
                        continue

                    resp.raise_for_status()
                    result = await resp.json()

                if "error" in result:
                    raise RuntimeError(f"RPC Error: {result['error']}")
                return result.get("result")

            except aiohttp.ClientError:
                if attempt == MAX_RETRIES - 1:
                    raise
                wait_time = RETRY_DELAY_BASE * (2 ** min(attempt, 4))
                await asyncio.sleep(wait_time)

        raise RuntimeError("Max retries exceeded")

    async def get_signatures_for_address(
        self, address: str, limit: int = 1000, before: Optional[str] = None
    ) -> List[Dict]:
        params_dict = {"limit": limit}
        if before:
            params_dict["before"] = before
        params = [address, params_dict]
        res = await self._make_request("getSignaturesForAddress", params)
        return res if res else []

    async def get_transaction(self, signature: str) -> Optional[Dict]:
        params = [
            signature,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
            },
        ]
        return await self._make_request("getTransaction", params)

    async def get_token_accounts_by_owner(self, owner: str, mint: str) -> List[Dict]:
        params = [
            owner,
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ]
        res = await self._make_request("getTokenAccountsByOwner", params)
        return res.get("value", []) if res else []

    async def get_token_supply(self, mint: str) -> Optional[Dict]:
        params = [mint, {"commitment": "finalized"}]
        res = await self._make_request("getTokenSupply", params)
        return res.get("value") if res else None


def _account_keys(tx: Dict) -> List[str]:
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    resolved = []
    for key in keys:
        if isinstance(key, dict):
            resolved.append(key.get("pubkey"))
        else:
            resolved.append(key)
    return resolved


def _token_account_map(tx: Dict) -> Dict[str, Dict]:
    keys = _account_keys(tx)
    token_balances = []
    meta = tx.get("meta", {})
    token_balances.extend(meta.get("preTokenBalances", []))
    token_balances.extend(meta.get("postTokenBalances", []))

    mapping: Dict[str, Dict] = {}
    for entry in token_balances:
        idx = entry.get("accountIndex")
        if idx is None or idx >= len(keys):
            continue
        account_pubkey = keys[idx]
        ui_amount = entry.get("uiTokenAmount", {})
        mapping[account_pubkey] = {
            "mint": entry.get("mint"),
            "owner": entry.get("owner"),
            "decimals": ui_amount.get("decimals"),
        }
    return mapping


def _iter_token_transfer_instructions(tx: Dict) -> Iterable[Dict]:
    msg = tx.get("transaction", {}).get("message", {})
    instructions = list(msg.get("instructions", []))
    for inner in tx.get("meta", {}).get("innerInstructions", []):
        instructions.extend(inner.get("instructions", []))

    for inst in instructions:
        parsed = inst.get("parsed")
        if not parsed:
            continue
        if inst.get("program") != "spl-token":
            continue
        inst_type = parsed.get("type")
        if inst_type not in {"transfer", "transferChecked"}:
            continue
        yield inst


def _iter_system_transfer_instructions(tx: Dict) -> Iterable[Dict]:
    msg = tx.get("transaction", {}).get("message", {})
    instructions = list(msg.get("instructions", []))
    for inner in tx.get("meta", {}).get("innerInstructions", []):
        instructions.extend(inner.get("instructions", []))

    for inst in instructions:
        parsed = inst.get("parsed")
        if not parsed:
            continue
        if inst.get("program") != "system":
            continue
        if parsed.get("type") != "transfer":
            continue
        yield inst


def _parse_transfer(
    inst: Dict, token_account_map: Dict[str, Dict]
) -> Optional[Tuple[str, str, int, int, str]]:
    parsed = inst.get("parsed", {})
    info = parsed.get("info", {})
    inst_type = parsed.get("type")

    source = info.get("source")
    destination = info.get("destination")
    if not source or not destination:
        return None

    mint = info.get("mint")
    amount_raw: Optional[int] = None
    decimals: Optional[int] = None

    if inst_type == "transferChecked":
        token_amount = info.get("tokenAmount", {})
        amount_raw = int(token_amount.get("amount", "0"))
        decimals = token_amount.get("decimals")
        mint = mint or token_amount.get("mint")
    else:
        amount_raw = int(info.get("amount", "0"))

    if not mint:
        mint = token_account_map.get(destination, {}).get("mint")

    if decimals is None:
        decimals = token_account_map.get(destination, {}).get("decimals")

    if not mint or decimals is None:
        return None

    return source, destination, amount_raw, decimals, mint


def _ui_amount(amount_raw: int, decimals: int) -> float:
    return amount_raw / (10 ** decimals)


def _to_iso(block_time: Optional[int]) -> Optional[str]:
    if not block_time:
        return None
    return datetime.fromtimestamp(block_time, tz=timezone.utc).isoformat()


def _to_ts_seconds(value: Optional[object]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            normalized = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            return None
    return None


async def track_incoming_multi(
    target_wallet: str,
    api_key: str,
    fartboy_mint: str,
    addresses: List[str],
    checkpoint_map: Dict[str, Optional[str]],
    max_pages: Optional[int],
    page_limit: int,
    request_delay: float,
) -> Tuple[List[Dict], Dict[str, Optional[str]]]:
    results: List[Dict] = []
    allowed_spl_mints = {fartboy_mint, USDC_MINT, USDT_MINT}
    new_checkpoint_map: Dict[str, Optional[str]] = {}
    signatures_to_process: List[str] = []

    async def collect_new_signatures(
        client: HeliusRPCClient, address: str, checkpoint_sig: Optional[str]
    ) -> Tuple[List[str], Optional[str]]:
        before_sig: Optional[str] = None
        new_checkpoint: Optional[str] = None
        collected: List[str] = []
        page_idx = 0
        while True:
            if max_pages is not None and page_idx >= max_pages:
                break
            sigs = await client.get_signatures_for_address(
                address, limit=page_limit, before=before_sig
            )
            if not sigs:
                break
            if page_idx == 0:
                new_checkpoint = sigs[0].get("signature") or new_checkpoint
                if checkpoint_sig is None:
                    return [], new_checkpoint
            for sig in sigs:
                signature = sig.get("signature")
                if not signature:
                    continue
                if checkpoint_sig and signature == checkpoint_sig:
                    return collected, new_checkpoint
                collected.append(signature)
            before_sig = sigs[-1].get("signature")
            if not before_sig:
                break
            page_idx += 1
        return collected, new_checkpoint

    async with HeliusRPCClient(api_key, request_delay=request_delay) as client:
        for address in addresses:
            collected, new_checkpoint = await collect_new_signatures(
                client, address, checkpoint_map.get(address)
            )
            new_checkpoint_map[address] = new_checkpoint or checkpoint_map.get(address)
            signatures_to_process.extend(collected)

        for signature in set(signatures_to_process):
            tx = await client.get_transaction(signature)
            if not tx:
                continue

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

                results.append(
                    {
                        "signature": signature,
                        "timestamp": _to_iso(tx.get("blockTime")),
                        "amount_raw": lamports,
                        "amount_ui": _ui_amount(lamports, SOL_DECIMALS),
                        "token": "SOL",
                        "sender_wallet": source,
                    }
                )

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

                results.append(
                    {
                        "signature": signature,
                        "timestamp": _to_iso(tx.get("blockTime")),
                        "amount_raw": amount_raw,
                        "amount_ui": _ui_amount(amount_raw, decimals),
                        "decimals": decimals,
                        "token": token_label,
                        "sender_wallet": source_owner,
                    }
                )

    return results, new_checkpoint_map


def _print_results(rows: List[Dict]) -> None:
    for row in rows:
        print(
            f"{row['timestamp']}\t{row['amount_ui']}\t{row['token']}\t"
            f"{row['sender_wallet']}"
        )


def _init_transactions_db(conn: sqlite3.Connection, table: str) -> None:
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signature TEXT NOT NULL,
            timestamp TEXT,
            sender_wallet TEXT NOT NULL,
            token TEXT NOT NULL,
            amount_raw INTEGER NOT NULL,
            amount_ui REAL NOT NULL,
            value_usdc REAL NOT NULL,
            value_fartboy REAL NOT NULL,
            discord_id TEXT
        )
        """
    )
    cur.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_uniq
        ON {table} (signature, sender_wallet, token, amount_raw)
        """
    )
    existing_cols = {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}
    if "discord_id" not in existing_cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN discord_id TEXT")
    conn.commit()


def _init_snapshot_db(conn: sqlite3.Connection, table: str) -> None:
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            wallet_address TEXT PRIMARY KEY,
            amount_fartboy REAL NOT NULL,
            donated_fartboy REAL NOT NULL,
            donated_usd REAL NOT NULL,
            discord_id TEXT,
            discord_name TEXT,
            on_leaderboard INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    existing_cols = {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}
    if "donated_fartboy" not in existing_cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN donated_fartboy REAL NOT NULL DEFAULT 0")
    if "donated_usd" not in existing_cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN donated_usd REAL NOT NULL DEFAULT 0")
    if "discord_id" not in existing_cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN discord_id TEXT")
    if "discord_name" not in existing_cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN discord_name TEXT")
    if "on_leaderboard" not in existing_cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN on_leaderboard INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def _ensure_otp_registry(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {OTP_TABLE} (
            otp_value TEXT NOT NULL,
            tick_size INTEGER NOT NULL,
            status TEXT NOT NULL,
            assigned_to_discord_id TEXT,
            assigned_to_name TEXT,
            assigned_at TEXT,
            used_at TEXT,
            tx_signature TEXT,
            sender_wallet TEXT,
            PRIMARY KEY (otp_value, tick_size)
        )
        """
    )
    existing_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({OTP_TABLE})")}
    if "sender_wallet" not in existing_cols:
        conn.execute(f"ALTER TABLE {OTP_TABLE} ADD COLUMN sender_wallet TEXT")
    conn.commit()




def _expire_otps(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        UPDATE {OTP_TABLE}
        SET status = 'expired'
        WHERE status = 'assigned'
          AND assigned_at IS NOT NULL
          AND assigned_at <= datetime('now', '-{OTP_EXPIRY_SECONDS} seconds')
        """
    )
    conn.commit()


def _format_otp_if_exact(amount_raw: int, decimals: int, tick: int) -> Optional[str]:
    amount = Decimal(amount_raw) / (Decimal(10) ** decimals)
    quantum = Decimal("1").scaleb(-tick)
    quantized = amount.quantize(quantum, rounding=ROUND_DOWN)
    if quantized != amount:
        return None
    return f"{quantized:.{tick}f}"


def _record_used_otp(
    conn: sqlite3.Connection,
    otp_value: str,
    tick_size: int,
    signature: str,
    sender_wallet: str,
) -> None:
    conn.execute(
        f"""
        INSERT INTO {OTP_TABLE} (
            otp_value, tick_size, status, used_at, tx_signature, sender_wallet
        )
        VALUES (?, ?, 'used', datetime('now'), ?, ?)
        ON CONFLICT(otp_value, tick_size) DO UPDATE SET
            status = 'used',
            used_at = datetime('now'),
            tx_signature = excluded.tx_signature,
            sender_wallet = excluded.sender_wallet
        """,
        (otp_value, tick_size, signature, sender_wallet),
    )
    conn.commit()


def _load_exchange_wallets(conn: sqlite3.Connection) -> Set[str]:
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='exchange_wallets'"
        ).fetchone()
        if not table:
            return set()
        rows = conn.execute(
            "SELECT wallet_address FROM exchange_wallets"
        ).fetchall()
        return {row[0] for row in rows if row and row[0]}
    except sqlite3.Error:
        return set()


def _match_assigned_otp(
    conn: sqlite3.Connection,
    otp_value: str,
    tick_size: int,
    signature: str,
    sender_wallet: str,
) -> Optional[Tuple[str, str]]:
    row = conn.execute(
        f"""
        SELECT assigned_to_discord_id, assigned_to_name
        FROM {OTP_TABLE}
        WHERE otp_value = ? AND tick_size = ? AND status = 'assigned'
        """,
        (otp_value, tick_size),
    ).fetchone()
    if not row:
        return None
    conn.execute(
        f"""
        UPDATE {OTP_TABLE}
        SET status = 'used',
            used_at = datetime('now'),
            tx_signature = ?,
            sender_wallet = ?
        WHERE otp_value = ? AND tick_size = ?
        """,
        (signature, sender_wallet, otp_value, tick_size),
    )
    conn.commit()
    return row[0], row[1] or ""


def _recompute_summary_for_discord_id(conn: sqlite3.Connection, discord_id: str) -> None:
    wallets = conn.execute(
        """
        SELECT wallet_address, amount_fartboy, donated_usd, discord_name
        FROM fartboy_holders
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


def _apply_donations(
    snapshot_conn: sqlite3.Connection,
    table: str,
    sender_wallet: str,
    value_fartboy: float,
    value_usdc: float,
) -> None:
    cur = snapshot_conn.cursor()
    cur.execute(
        f"""
        INSERT INTO {table} (wallet_address, amount_fartboy, donated_fartboy, donated_usd)
        VALUES (?, 0, ?, ?)
        ON CONFLICT(wallet_address) DO UPDATE SET
            donated_fartboy = donated_fartboy + excluded.donated_fartboy,
            donated_usd = donated_usd + excluded.donated_usd
        """,
        (sender_wallet, value_fartboy, value_usdc),
    )
    snapshot_conn.commit()


def _calculate_values(
    token: str,
    amount_ui: float,
    fartboy_usdc_price: float,
    token_usdc_price: float,
) -> Tuple[float, float]:
    if token == "FARTBOY":
        value_usdc = amount_ui * fartboy_usdc_price
        value_fartboy = amount_ui
    elif token in ("USDC", "USDT"):
        value_usdc = amount_ui * token_usdc_price
        value_fartboy = value_usdc / fartboy_usdc_price if fartboy_usdc_price > 0 else 0.0
    else:
        value_usdc = amount_ui * token_usdc_price
        value_fartboy = value_usdc / fartboy_usdc_price if fartboy_usdc_price > 0 else 0.0
    return value_usdc, value_fartboy


def _load_checkpoint_map(path: str) -> Dict[str, Optional[str]]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="ascii") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            return {k: (v or None) for k, v in payload.items()}
        return {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_checkpoint_map(path: str, payload: Dict[str, Optional[str]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii") as handle:
        json.dump(payload, handle)


class IncomingTracker:
    def __init__(
        self,
        api_key: str,
        target_wallet: str,
        fartboy_mint: str,
        tx_db: str = "data/incoming_transactions.db",
        tx_table: str = "incoming_transactions",
        snapshot_db: str = "data/fartboy_snapshot.db",
        snapshot_table: str = "fartboy_holders",
        checkpoint_file: str = "data/incoming_checkpoint.txt",
        delay: float = 0.1,
        max_pages: Optional[int] = 3,
        page_limit: int = 10,
        account_refresh_seconds: int = 0,
    ) -> None:
        self.api_key = api_key
        self.target_wallet = target_wallet
        self.fartboy_mint = fartboy_mint
        self.tx_db = tx_db
        self.tx_table = tx_table
        self.snapshot_db = snapshot_db
        self.snapshot_table = snapshot_table
        self.checkpoint_file = checkpoint_file
        self.delay = max(0.05, delay)
        if max_pages is None or max_pages <= 0:
            self.max_pages = None
        else:
            self.max_pages = max_pages
        self.page_limit = min(max(page_limit, 1), 1000)
        self.account_refresh_seconds = max(30, account_refresh_seconds)
        self._last_accounts_refresh = 0.0
        self._cached_addresses: List[str] = []
        self.unlimited_backfill = False

    async def _get_tracked_addresses(self) -> List[str]:
        now = time.time()
        if self._cached_addresses and (
            self.account_refresh_seconds <= 0
            or (now - self._last_accounts_refresh) < self.account_refresh_seconds
        ):
            return list(self._cached_addresses)
        addresses = [self.target_wallet]
        async with HeliusRPCClient(self.api_key, request_delay=self.delay) as client:
            fartboy_accounts = await client.get_token_accounts_by_owner(
                self.target_wallet, self.fartboy_mint
            )
            usdc_accounts = await client.get_token_accounts_by_owner(
                self.target_wallet, USDC_MINT
            )
            usdt_accounts = await client.get_token_accounts_by_owner(
                self.target_wallet, USDT_MINT
            )
        for acc in fartboy_accounts + usdc_accounts + usdt_accounts:
            pubkey = acc.get("pubkey")
            if pubkey:
                addresses.append(pubkey)
        self._cached_addresses = addresses
        self._last_accounts_refresh = now
        return list(addresses)

    async def run_once(self) -> Tuple[int, List[str], List[str]]:
        checkpoint_map = _load_checkpoint_map(self.checkpoint_file)
        addresses = await self._get_tracked_addresses()

        max_pages = None if self.unlimited_backfill else self.max_pages
        rows, new_checkpoint_map = await track_incoming_multi(
            target_wallet=self.target_wallet,
            api_key=self.api_key,
            fartboy_mint=self.fartboy_mint,
            addresses=addresses,
            checkpoint_map=checkpoint_map,
            max_pages=max_pages,
            page_limit=self.page_limit,
            request_delay=self.delay,
        )
        if new_checkpoint_map and new_checkpoint_map != checkpoint_map:
            _save_checkpoint_map(self.checkpoint_file, new_checkpoint_map)
            checkpoint_map = new_checkpoint_map
        if not checkpoint_map and not rows:
            print("Checkpoint initialized; no historical transactions processed.")
            return 0, [], []
        if not rows:
            print("No new incoming transactions.")
            return 0, [], []

        _print_results(rows)
        print(f"Found {len(rows)} incoming transactions.")

        tx_dir = os.path.dirname(self.tx_db)
        if tx_dir:
            os.makedirs(tx_dir, exist_ok=True)
        snapshot_dir = os.path.dirname(self.snapshot_db)
        if snapshot_dir:
            os.makedirs(snapshot_dir, exist_ok=True)
        tx_conn = sqlite3.connect(self.tx_db)
        snapshot_conn = sqlite3.connect(self.snapshot_db)
        try:
            _init_transactions_db(tx_conn, self.tx_table)
            _init_snapshot_db(snapshot_conn, self.snapshot_table)
            _ensure_otp_registry(snapshot_conn)
            _expire_otps(snapshot_conn)
            exchange_wallets = _load_exchange_wallets(snapshot_conn)

            computed = await _compute_values_for_rows(rows, self.fartboy_mint)
            if not computed:
                print("Price lookup failed; skipping donation updates this cycle.")
                return len(rows), [], []

            sender_wallets: List[str] = []
            verified_discord_ids: List[str] = []
            for row, value_usdc, value_fartboy in computed:
                cur = tx_conn.cursor()
                cur.execute(
                    f"""
                    INSERT OR IGNORE INTO {self.tx_table} (
                        signature, timestamp, sender_wallet, token, amount_raw,
                        amount_ui, value_usdc, value_fartboy
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["signature"],
                        row["timestamp"],
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
                    sender_wallets.append(row["sender_wallet"])
                    _apply_donations(
                        snapshot_conn,
                        self.snapshot_table,
                        row["sender_wallet"],
                        value_fartboy,
                        value_usdc,
                    )
                    if row.get("token") == "FARTBOY" and row.get("amount_ui", 0) < 1:
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
                                verified_discord_ids.append(discord_id)
                                snapshot_conn.execute(
                                    f"""
                                    UPDATE {self.snapshot_table}
                                    SET discord_id = ?, discord_name = ?, on_leaderboard = 0
                                    WHERE wallet_address = ?
                                      AND (discord_id IS NULL OR discord_id = ?)
                                    """,
                                    (discord_id, discord_name, row["sender_wallet"], discord_id),
                                )
                                snapshot_conn.commit()
                                _recompute_summary_for_discord_id(snapshot_conn, discord_id)
                            else:
                                _record_used_otp(
                                    snapshot_conn,
                                    otp_value,
                                    tick,
                                    row["signature"],
                                    row["sender_wallet"],
                                )
        finally:
            tx_conn.close()
            snapshot_conn.close()
        return len(rows), sender_wallets, verified_discord_ids

    async def init_checkpoint(self) -> int:
        addresses = await self._get_tracked_addresses()
        checkpoint_map: Dict[str, Optional[str]] = {}
        async with HeliusRPCClient(self.api_key, request_delay=self.delay) as client:
            for address in addresses:
                sigs = await client.get_signatures_for_address(address, limit=1)
                latest = sigs[0].get("signature") if sigs else None
                checkpoint_map[address] = latest or None
        _save_checkpoint_map(self.checkpoint_file, checkpoint_map)
        return len(addresses)


async def _fetch_jupiter_price_usdc(session: aiohttp.ClientSession, mint: str) -> float:
    params = f"ids={mint}&vsToken={USDC_MINT}"
    url = f"https://price.jup.ag/v6/price?{params}"
    async with session.get(url) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    data = payload.get("data", {})
    price = data.get(mint, {}).get("price")
    if price is None:
        raise RuntimeError(f"Missing price for mint {mint}")
    return float(price)


async def _fetch_coingecko_price_usd(session: aiohttp.ClientSession, mint: str) -> float:
    if mint == SOL_MINT:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
        async with session.get(url) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        price = payload.get("solana", {}).get("usd")
        return float(price) if price is not None else 0.0

    params = f"contract_addresses={mint}&vs_currencies=usd"
    url = f"https://api.coingecko.com/api/v3/simple/token_price/solana?{params}"
    async with session.get(url) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    entry = payload.get(mint) or payload.get(mint.lower()) or {}
    price = entry.get("usd")
    return float(price) if price is not None else 0.0


def _nearest_price(prices: List[List[float]], ts_seconds: int) -> float:
    if not prices:
        return 0.0
    target_ms = ts_seconds * 1000
    best = min(prices, key=lambda p: abs(p[0] - target_ms))
    return float(best[1]) if len(best) > 1 else 0.0


async def _fetch_coingecko_price_range(
    session: aiohttp.ClientSession, mint: str, start: int, end: int
) -> List[List[float]]:
    if mint == SOL_MINT:
        url = (
            "https://api.coingecko.com/api/v3/coins/solana/market_chart/range"
            f"?vs_currency=usd&from={start}&to={end}"
        )
        async with session.get(url) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        return payload.get("prices", [])

    url = (
        "https://api.coingecko.com/api/v3/coins/solana/contract/"
        f"{mint}/market_chart/range?vs_currency=usd&from={start}&to={end}"
    )
    async with session.get(url) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    return payload.get("prices", [])


async def _compute_values_for_rows(rows: List[Dict], fartboy_mint: str) -> List[Tuple[Dict, float, float]]:
    results: List[Tuple[Dict, float, float]] = []
    async with aiohttp.ClientSession() as session:
        price_cache: Dict[str, float] = {}
        range_cache: Dict[str, List[List[float]]] = {}

        timestamps = [
            ts
            for ts in (_to_ts_seconds(row.get("timestamp")) for row in rows)
            if ts is not None
        ]
        range_start = min(timestamps) - 600 if timestamps else None
        range_end = max(timestamps) + 600 if timestamps else None

        async def get_range(mint: str) -> List[List[float]]:
            if mint in range_cache:
                return range_cache[mint]
            if range_start is None or range_end is None:
                range_cache[mint] = []
                return []
            try:
                prices = await _fetch_coingecko_price_range(session, mint, range_start, range_end)
            except aiohttp.ClientError:
                prices = []
            range_cache[mint] = prices
            return prices
        async def get_price(mint: str) -> float:
            if mint in price_cache:
                return price_cache[mint]
            price = 0.0
            try:
                price = await _fetch_jupiter_price_usdc(session, mint)
            except aiohttp.ClientError:
                price = 0.0
            if price <= 0:
                try:
                    price = await _fetch_coingecko_price_usd(session, mint)
                except aiohttp.ClientError:
                    price = 0.0
            price_cache[mint] = price
            return price

        async def get_price_at(mint: str, ts_value: Optional[object]) -> float:
            ts_seconds = _to_ts_seconds(ts_value)
            if ts_seconds is None:
                return await get_price(mint)
            cache_key = f"{mint}:{ts_seconds // 60}"
            if cache_key in price_cache:
                return price_cache[cache_key]
            price = 0.0
            prices = await get_range(mint)
            if prices:
                price = _nearest_price(prices, ts_seconds)
            if price <= 0:
                price = await get_price(mint)
            price_cache[cache_key] = price
            return price

        for row in rows:
            token = row["token"]
            if token in ("USDC", "USDT"):
                token_price = 1.0
            elif token == "SOL":
                token_price = await get_price_at(SOL_MINT, row.get("timestamp"))
            else:
                token_price = await get_price_at(fartboy_mint, row.get("timestamp"))

            fartboy_price = await get_price_at(fartboy_mint, row.get("timestamp"))
            if fartboy_price <= 0:
                # Skip pricing if we can't reach the price API.
                continue
            value_usdc, value_fartboy = _calculate_values(
                token, row["amount_ui"], fartboy_price, token_price
            )
            results.append((row, value_usdc, value_fartboy))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Track incoming FARTBOY + USDC + USDT (SPL) and SOL transfers to a wallet."
    )
    parser.add_argument(
        "--wallet",
        default=None,
        help="Target wallet address to track (or WARCHEST_ADDRESS env var)",
    )
    parser.add_argument("--api-key", default=None, help="Helius API key (or HELIUS_API_KEY env var)")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="Max pages to scan (0 = no limit)",
    )
    parser.add_argument("--page-limit", type=int, default=10, help="Signatures per page (max 1000)")
    parser.add_argument("--delay", type=float, default=0.1, help="Delay between RPC requests (seconds)")
    parser.add_argument("--tx-db", default="data/incoming_transactions.db", help="Transactions SQLite DB")
    parser.add_argument("--tx-table", default="incoming_transactions", help="Transactions table")
    parser.add_argument("--snapshot-db", default="data/fartboy_snapshot.db", help="Snapshot SQLite DB")
    parser.add_argument("--snapshot-table", default="fartboy_holders", help="Snapshot table")
    parser.add_argument(
        "--checkpoint-file",
        default="data/incoming_checkpoint.txt",
        help="Checkpoint file storing last processed signature",
    )
    parser.add_argument("--interval", type=int, default=0, help="Run every N seconds (0 = once)")
    args = parser.parse_args()

    api_key = args.api_key or os.getenv("HELIUS_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: Missing HELIUS_API_KEY (or pass --api-key).")

    target_wallet = args.wallet or os.getenv("WARCHEST_ADDRESS")
    if not target_wallet:
        raise SystemExit(
            "ERROR: Missing wallet. Use --wallet or set WARCHEST_ADDRESS."
        )

    fartboy_mint = os.getenv("FARTBOY_MINT")
    if not fartboy_mint:
        raise SystemExit("ERROR: Missing FARTBOY_MINT in environment.")
    max_pages = None if args.max_pages <= 0 else args.max_pages
    tracker = IncomingTracker(
        api_key=api_key,
        target_wallet=target_wallet,
        fartboy_mint=fartboy_mint,
        tx_db=args.tx_db,
        tx_table=args.tx_table,
        snapshot_db=args.snapshot_db,
        snapshot_table=args.snapshot_table,
        checkpoint_file=args.checkpoint_file,
        delay=args.delay,
        max_pages=max_pages,
        page_limit=args.page_limit,
    )

    if args.interval <= 0:
        asyncio.run(tracker.run_once())
        return

    interval = max(5, args.interval)
    while True:
        asyncio.run(tracker.run_once())
        time.sleep(interval)


if __name__ == "__main__":
    main()
