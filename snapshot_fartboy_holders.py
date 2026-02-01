#!/usr/bin/env python3
"""
Snapshot all wallets holding a specific SPL token (FARTBOY) into SQLite.

Stores per-wallet:
- wallet_address
- amount_fartboy (UI amount)
- donated_fartboy (UI amount, default 0)
"""

import argparse
import asyncio
import base64
import os
import sqlite3
import struct
import time
from typing import Dict, List, Optional, Tuple

import aiohttp
import base58
from dotenv import load_dotenv

load_dotenv()

SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_ACCOUNT_SIZE = 165
MINT_OFFSET = 0
OWNER_OFFSET = 32
AMOUNT_OFFSET = 64

MAX_RETRIES = 6
RETRY_DELAY_BASE = 1.0


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

    async def get_program_accounts(self, program_id: str, filters: List[Dict]) -> List[Dict]:
        params = [program_id, {"filters": filters, "encoding": "base64"}]
        res = await self._make_request("getProgramAccounts", params)
        return res if res else []

    async def get_multiple_accounts(self, addresses: List[str], encoding: str = "base64") -> List[Dict]:
        params = [addresses, {"encoding": encoding}]
        res = await self._make_request("getMultipleAccounts", params)
        return res.get("value", []) if res else []


def encode_base58(data: bytes) -> str:
    return base58.b58encode(data).decode("ascii")


def parse_token_account(data_field, expected_mint: str) -> Optional[Tuple[str, int]]:
    try:
        if isinstance(data_field, list):
            data_b64 = data_field[0] if data_field else None
        elif isinstance(data_field, str):
            data_b64 = data_field
        else:
            return None

        if not data_b64:
            return None

        data = base64.b64decode(data_b64)
        if len(data) < AMOUNT_OFFSET + 8:
            return None

        mint_bytes = data[MINT_OFFSET:MINT_OFFSET + 32]
        owner_bytes = data[OWNER_OFFSET:OWNER_OFFSET + 32]
        amount_bytes = data[AMOUNT_OFFSET:AMOUNT_OFFSET + 8]

        mint = encode_base58(mint_bytes)
        if mint != expected_mint:
            return None

        owner = encode_base58(owner_bytes)
        amount_raw = int(struct.unpack("<Q", amount_bytes)[0])
        return owner, amount_raw
    except Exception:
        return None


async def fetch_decimals(client: HeliusRPCClient, mint_address: str) -> Optional[int]:
    try:
        accounts = await client.get_multiple_accounts([mint_address], encoding="base64")
        if not accounts or not accounts[0]:
            return None

        account_data = accounts[0].get("data", [])
        if isinstance(account_data, list):
            data_b64 = account_data[0] if account_data else None
        elif isinstance(account_data, str):
            data_b64 = account_data
        else:
            return None

        if not data_b64:
            return None

        data = base64.b64decode(data_b64)
        if len(data) < 45:
            return None

        return data[44]
    except Exception:
        return None


async def fetch_balances_by_owner(
    mint_address: str, api_key: str, request_delay: float
) -> Dict[str, int]:
    filters = [
        {"dataSize": TOKEN_ACCOUNT_SIZE},
        {"memcmp": {"offset": MINT_OFFSET, "bytes": mint_address}},
    ]

    async with HeliusRPCClient(api_key, request_delay=request_delay) as client:
        accounts = await client.get_program_accounts(SPL_TOKEN_PROGRAM_ID, filters)
        balances: Dict[str, int] = {}
        for acc in accounts:
            data_field = acc.get("account", {}).get("data")
            parsed = parse_token_account(data_field, expected_mint=mint_address)
            if not parsed:
                continue
            owner, amount_raw = parsed
            if amount_raw <= 0:
                continue
            balances[owner] = balances.get(owner, 0) + amount_raw
        return balances


def init_db(conn: sqlite3.Connection, table: str) -> None:
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


def write_snapshot(
    conn: sqlite3.Connection, table: str, balances_ui: Dict[str, float]
) -> None:
    cur = conn.cursor()
    rows = [(wallet, amount) for wallet, amount in balances_ui.items()]
    cur.executemany(
        f"""
        INSERT INTO {table} (wallet_address, amount_fartboy, donated_fartboy, donated_usd)
        VALUES (?, ?, 0, 0)
        ON CONFLICT(wallet_address) DO UPDATE SET
            amount_fartboy=excluded.amount_fartboy
        """,
        rows,
    )
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot FARTBOY holders into SQLite.")
    parser.add_argument("--mint", default=None, help="FARTBOY mint address (base58)")
    parser.add_argument("--db", default="data/fartboy_snapshot.db", help="SQLite database path")
    parser.add_argument("--table", default="fartboy_holders", help="Table name")
    parser.add_argument("--delay", type=float, default=0.1, help="Delay between RPC requests (seconds)")
    args = parser.parse_args()

    api_key = os.getenv("HELIUS_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: Missing HELIUS_API_KEY in environment.")

    mint_address = args.mint or os.getenv("FARTBOY_MINT")
    if not mint_address:
        raise SystemExit("ERROR: Missing mint address. Use --mint or set FARTBOY_MINT.")

    balances_raw = asyncio.run(
        fetch_balances_by_owner(
            mint_address=mint_address,
            api_key=api_key,
            request_delay=max(0.05, args.delay),
        )
    )

    async def _get_decimals() -> Optional[int]:
        async with HeliusRPCClient(api_key, request_delay=max(0.05, args.delay)) as client:
            return await fetch_decimals(client, mint_address)

    decimals = asyncio.run(_get_decimals())
    if decimals is None:
        raise SystemExit("ERROR: Could not fetch token decimals.")

    balances_ui = {w: amt / (10 ** decimals) for w, amt in balances_raw.items()}

    db_dir = os.path.dirname(args.db)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(args.db)
    try:
        init_db(conn, args.table)
        write_snapshot(conn, args.table, balances_ui)
    finally:
        conn.close()

    print(f"Snapshot written: {len(balances_ui)} wallets -> {args.db}:{args.table}")


if __name__ == "__main__":
    main()
