#!/usr/bin/env python3
"""
REST API server for exposing donation data to external websites.

Designed to run in-process with the Discord bot (started in a daemon thread).
Provides endpoints for stats, leaderboard, and recent transactions.
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Callable, List, Optional, Dict, Any, Tuple

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Database paths (same as bot)
SNAPSHOT_DB = os.getenv("SNAPSHOT_DB", "/app/data/fartboy_snapshot.db")
SNAPSHOT_TABLE = os.getenv("SNAPSHOT_TABLE", "fartboy_holders")
STATE_DB = os.getenv("LEADERBOARD_STATE_DB", "/app/data/leaderboard_state.db")
TX_DB = os.getenv("TX_DB", "/app/data/incoming_transactions.db")
TX_TABLE = os.getenv("TX_TABLE", "incoming_transactions")
SUMMARY_TABLE = os.getenv("SUMMARY_TABLE", "verified_users")


def _connect_db(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return sqlite3.connect(path)


def _format_wallet(wallet: str) -> str:
    """Truncate wallet address for display."""
    if not wallet or len(wallet) < 8:
        return wallet or ""
    return f"{wallet[:4]}...{wallet[-4:]}"


def _verify_api_key(api_key: Optional[str]) -> None:
    """Verify the API key from the request header."""
    expected = os.getenv("API_KEY")
    if not expected:
        raise HTTPException(status_code=500, detail="API key not configured on server.")
    if not api_key or api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


def _fetch_total_donations_usd() -> float:
    try:
        with sqlite3.connect(TX_DB) as conn:
            row = conn.execute(
                f"SELECT SUM(value_usdc) FROM {TX_TABLE}"
            ).fetchone()
        return float(row[0] or 0)
    except sqlite3.Error:
        return 0.0


def _fetch_total_by_token() -> Dict[str, float]:
    result = {"USDC": 0.0, "USDT": 0.0, "FARTBOY": 0.0, "SOL": 0.0}
    try:
        with sqlite3.connect(TX_DB) as conn:
            rows = conn.execute(
                f"SELECT token, SUM(value_usdc) FROM {TX_TABLE} GROUP BY token"
            ).fetchall()
        for token, total in rows:
            result[token] = float(total or 0)
    except sqlite3.Error:
        pass
    return result


def _get_or_create_anonymous_id(conn: sqlite3.Connection, discord_id: str) -> int:
    """Get existing anonymous_id or assign next available."""
    row = conn.execute(
        f"SELECT anonymous_id FROM {SUMMARY_TABLE} WHERE discord_id = ?",
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


def _fetch_leaderboard() -> Tuple[List[Dict[str, Any]], float]:
    """Fetch full leaderboard data, matching the Discord display exactly."""
    total_raised = _fetch_total_donations_usd()

    try:
        with _connect_db(SNAPSHOT_DB) as conn:
            summary_rows = conn.execute(
                f"""
                SELECT discord_id, discord_name, total_donated_usd, leaderboard_visible, anonymous_id
                FROM {SUMMARY_TABLE}
                """
            ).fetchall()
            snapshot_rows = conn.execute(
                f"""
                SELECT wallet_address, donated_usd, discord_id
                FROM {SNAPSHOT_TABLE}
                WHERE donated_usd > 0
                """
            ).fetchall()
            try:
                exchange_rows = conn.execute(
                    "SELECT wallet_address FROM exchange_wallets"
                ).fetchall()
            except sqlite3.OperationalError:
                exchange_rows = []

        exchange_wallets = {r[0] for r in exchange_rows if r and r[0]}
        combined: List[Tuple[str, float, Optional[str], Optional[str], int]] = []

        # Aggregate verified users using summary rows.
        for discord_id, discord_name, total_donated_usd, leaderboard_visible, anonymous_id in summary_rows:
            if not discord_id:
                continue
            if int(leaderboard_visible or 0) == 1:
                combined.append(
                    ("", float(total_donated_usd or 0), discord_id, discord_name, 1)
                )
            else:
                anon_id = int(anonymous_id or 0)
                if not anon_id:
                    with _connect_db(SNAPSHOT_DB) as conn2:
                        anon_id = _get_or_create_anonymous_id(conn2, discord_id)
                combined.append(
                    (
                        f"Anonymous donor {anon_id}",
                        float(total_donated_usd or 0),
                        None,
                        None,
                        0,
                    )
                )

        # Add unverified wallets.
        for wallet, donated_usd, discord_id in snapshot_rows:
            if discord_id:
                continue
            if wallet in exchange_wallets:
                continue
            combined.append((wallet, float(donated_usd or 0), None, None, 0))

        combined.sort(key=lambda r: r[1], reverse=True)

    except sqlite3.Error:
        combined = []

    # Build leaderboard entries matching Discord format.
    entries = []
    for idx, (wallet, donated_usd, discord_id, discord_name, on_leaderboard) in enumerate(combined, start=1):
        is_anonymous = not discord_id or int(on_leaderboard or 0) == 0

        if discord_id and discord_name:
            display_name = discord_name
        elif discord_id:
            display_name = f"User#{discord_id[-4:]}"
        elif wallet and (" " in wallet or wallet.startswith("Anonymous donor")):
            display_name = wallet
        else:
            display_name = _format_wallet(wallet)

        entries.append({
            "rank": idx,
            "display_name": display_name,
            "donated_usd": round(float(donated_usd or 0), 2),
            "is_anonymous": is_anonymous,
        })

    return entries, total_raised


def _fetch_targets(chest_value: float) -> Tuple[List[Dict], Optional[Dict]]:
    """Fetch all active targets and identify the next one."""
    targets = []
    next_target = None
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
        for r in rows:
            target_amount = float(r[1])
            completed = r[5] is not None
            progress = (chest_value / target_amount * 100) if target_amount > 0 else 0
            progress = min(100.0, progress)
            t = {
                "id": r[0],
                "target_amount": target_amount,
                "target_name": r[2],
                "completed": completed,
                "completed_at": r[5],
                "progress_percent": round(progress, 1),
            }
            targets.append(t)
            if not completed and next_target is None:
                next_target = t
    except sqlite3.Error:
        pass
    return targets, next_target


def _fetch_recent_transactions(limit: int = 20) -> List[Dict[str, Any]]:
    """Fetch recent transactions."""
    try:
        with sqlite3.connect(TX_DB) as conn:
            rows = conn.execute(
                f"""
                SELECT timestamp, amount_ui, token, value_usdc, sender_wallet
                FROM {TX_TABLE}
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "timestamp": ts,
                "amount_ui": float(amount_ui or 0),
                "token": token or "UNKNOWN",
                "value_usdc": round(float(value_usdc or 0), 6),
                "sender_wallet": _format_wallet(sender_wallet or ""),
            }
            for ts, amount_ui, token, value_usdc, sender_wallet in rows
        ]
    except sqlite3.Error:
        return []


def create_api_app(chest_value_getter: Callable[[], float] = lambda: 0.0) -> FastAPI:
    """Factory function to create the FastAPI application."""
    app = FastAPI(
        title="Chest Bot API",
        description="API for accessing donation and leaderboard data.",
        version="1.0.0",
        docs_url=None,       # Disable public /docs
        redoc_url=None,      # Disable public /redoc
        openapi_url=None,    # Disable public /openapi.json
    )

    # CORS
    origins = os.getenv("API_CORS_ORIGINS", "*").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins],
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.get("/")
    async def root():
        return {"status": "ok"}

    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    @app.get("/openapi.json", include_in_schema=False)
    async def get_openapi(
        x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        api_key_query: Optional[str] = Query(None, alias="X-API-Key"),
    ):
        _verify_api_key(x_api_key or api_key_query)
        return JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False)
    async def get_docs(
        x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        key: Optional[str] = Query(None),
    ):
        resolved_key = x_api_key or key
        _verify_api_key(resolved_key)
        return get_swagger_ui_html(
            openapi_url=f"/openapi.json?X-API-Key={resolved_key}",
            title="Chest Bot API - Docs",
        )

    @app.get("/api/v1/stats")
    async def get_stats(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
        """
        Overall statistics.

        Returns total raised (USD), current chest value, breakdown by token
        (USDC, USDT, FARTBOY, SOL), all active targets with progress, and the
        next uncompleted target. Target progress is based on current chest value
        (matching the Discord leaderboard display).

        **Data types & boundaries:**

        | Field | Type | Range | Description |
        |---|---|---|---|
        | total_raised_usd | float | [0, ∞) | Sum of all donations in USD |
        | chest_value_usd | float | [0, ∞) | Current chest wallet value in USD |
        | raised_by_token.* | float | [0, ∞) | USD raised per token type |
        | targets[].target_amount | float | (0, ∞) | Milestone amount in USD |
        | targets[].progress_percent | float | [0, 100] | % progress toward target (based on chest value) |
        | next_target | object or null | — | First uncompleted target |
        """
        _verify_api_key(x_api_key)

        total_raised = _fetch_total_donations_usd()
        by_token = _fetch_total_by_token()

        chest_value = chest_value_getter()
        targets, next_target = _fetch_targets(chest_value)

        return {
            "total_raised_usd": round(total_raised, 2),
            "chest_value_usd": round(chest_value, 2),
            "raised_by_token": {k: round(v, 2) for k, v in by_token.items()},
            "targets": targets,
            "next_target": next_target,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/v1/leaderboard")
    async def get_leaderboard(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
        """
        Full donor leaderboard — identical to what is shown on Discord.

        Returns every donor entry with rank, display name, donated USD, and
        whether the entry is anonymous. Discord usernames are used for verified
        donors who opted in; anonymous donors are labelled "Anonymous donor N".

        **Data types & boundaries:**

        | Field | Type | Range | Description |
        |---|---|---|---|
        | total_raised_usd | float | [0, ∞) | Grand total |
        | entries[].rank | int | [1, ∞) | Position on leaderboard |
        | entries[].display_name | string | 1-50 chars | Discord username or anon label |
        | entries[].donated_usd | float | [0, ∞) | Total donated in USD |
        | entries[].is_anonymous | bool | — | Whether donor is anonymous |
        """
        _verify_api_key(x_api_key)

        entries, total_raised = _fetch_leaderboard()

        return {
            "total_raised_usd": round(total_raised, 2),
            "entries": entries,
            "total_entries": len(entries),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/v1/recent")
    async def get_recent(
        limit: int = Query(default=20, ge=1, le=100, description="Number of transactions (1-100)"),
        x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    ):
        """
        Recent donation transactions.

        **Data types & boundaries:**

        | Field | Type | Range | Description |
        |---|---|---|---|
        | transactions[].timestamp | string (ISO) | — | When the tx occurred |
        | transactions[].amount_ui | float | [0, ∞) | Human-readable token amount |
        | transactions[].token | string | USDC/USDT/FARTBOY/SOL | Token type |
        | transactions[].value_usdc | float | [0, ∞) | USD value at time of tx |
        | transactions[].sender_wallet | string | 11 chars | Truncated wallet (privacy) |
        """
        _verify_api_key(x_api_key)

        transactions = _fetch_recent_transactions(limit)

        return {
            "transactions": transactions,
            "total_count": len(transactions),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

    return app
