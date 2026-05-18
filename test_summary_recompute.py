#!/usr/bin/env python3
"""Tests for verified_users summary recompute logic."""

import os
import sqlite3
import tempfile
import unittest

from incoming_tracker import (
    SUMMARY_TABLE,
    _init_snapshot_db,
    _init_transactions_db,
    _ensure_summary_table,
    recompute_summary_for_discord_id,
    recompute_summary_for_wallet,
    recompute_all_verified_summaries,
)

SNAPSHOT_TABLE = "fartboy_holders"
TX_TABLE = "incoming_transactions"
USER_ID = "123456789"
WALLET_A = "WalletAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WALLET_EX = "ExchangeBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


class SummaryRecomputeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.snapshot_db = os.path.join(self.tmp.name, "snapshot.db")
        self.tx_db = os.path.join(self.tmp.name, "tx.db")
        with sqlite3.connect(self.snapshot_db) as conn:
            _init_snapshot_db(conn, SNAPSHOT_TABLE)
            _ensure_summary_table(conn, SUMMARY_TABLE)
            conn.commit()
        with sqlite3.connect(self.tx_db) as conn:
            _init_transactions_db(conn, TX_TABLE)
            conn.commit()
        self.kw = dict(
            snapshot_db=self.snapshot_db,
            snapshot_table=SNAPSHOT_TABLE,
            tx_db_path=self.tx_db,
            tx_table=TX_TABLE,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _insert_wallet(
        self,
        wallet: str,
        *,
        discord_id: str | None = USER_ID,
        donated_usd: float = 0.0,
        donated_fartboy: float = 0.0,
        amount_fartboy: float = 1000.0,
    ) -> None:
        with sqlite3.connect(self.snapshot_db) as conn:
            conn.execute(
                f"""
                INSERT INTO {SNAPSHOT_TABLE} (
                    wallet_address, amount_fartboy, donated_fartboy, donated_usd,
                    discord_id, discord_name, on_leaderboard
                )
                VALUES (?, ?, ?, ?, ?, 'tester', 0)
                """,
                (wallet, amount_fartboy, donated_fartboy, donated_usd, discord_id,),
            )
            conn.commit()

    def _insert_tx(
        self,
        sender: str,
        value_usdc: float,
        *,
        discord_id: str | None = USER_ID,
        value_fartboy: float | None = None,
    ) -> None:
        if value_fartboy is None:
            value_fartboy = value_usdc
        with sqlite3.connect(self.tx_db) as conn:
            conn.execute(
                f"""
                INSERT INTO {TX_TABLE} (
                    signature, timestamp, sender_wallet, token, amount_raw,
                    amount_ui, value_usdc, value_fartboy, discord_id
                )
                VALUES (?, '2025-01-01', ?, 'FARTBOY', 1, 1, ?, ?, ?)
                """,
                (f"sig_{sender}_{value_usdc}", sender, value_usdc, value_fartboy, discord_id),
            )
            conn.commit()

    def _summary_total(self) -> float:
        with sqlite3.connect(self.snapshot_db) as conn:
            row = conn.execute(
                f"SELECT total_donated_usd FROM {SUMMARY_TABLE} WHERE discord_id = ?",
                (USER_ID,),
            ).fetchone()
        return float(row[0] or 0) if row else 0.0

    def _snapshot_donated(self, wallet: str) -> float:
        with sqlite3.connect(self.snapshot_db) as conn:
            row = conn.execute(
                f"SELECT donated_usd FROM {SNAPSHOT_TABLE} WHERE wallet_address = ?",
                (wallet,),
            ).fetchone()
        return float(row[0] or 0) if row else 0.0

    def test_snapshot_only_total(self) -> None:
        self._insert_wallet(WALLET_A, donated_usd=45.80, donated_fartboy=5000.0)
        recompute_summary_for_discord_id(USER_ID, **self.kw)
        self.assertAlmostEqual(self._summary_total(), 45.80, places=2)

    def test_stale_snapshot_tx_attributed_regression(self) -> None:
        """Snapshot donated_usd=0 but attributed tx exists — summary must not stay $0."""
        self._insert_wallet(WALLET_A, donated_usd=0.0)
        self._insert_tx(WALLET_A, 45.80)
        recompute_summary_for_discord_id(USER_ID, **self.kw)
        self.assertAlmostEqual(self._summary_total(), 45.80, places=2)
        self.assertAlmostEqual(self._snapshot_donated(WALLET_A), 45.80, places=2)

    def test_exchange_attribution_extra(self) -> None:
        self._insert_wallet(WALLET_A, donated_usd=45.80)
        self._insert_tx(WALLET_EX, 10.0)
        recompute_summary_for_discord_id(USER_ID, **self.kw)
        self.assertAlmostEqual(self._summary_total(), 55.80, places=2)

    def test_no_double_count_snapshot_and_tx_same_sender(self) -> None:
        self._insert_wallet(WALLET_A, donated_usd=45.80, donated_fartboy=5000.0)
        self._insert_tx(WALLET_A, 45.80, value_fartboy=5000.0)
        recompute_summary_for_discord_id(USER_ID, **self.kw)
        self.assertAlmostEqual(self._summary_total(), 45.80, places=2)

    def test_recompute_for_wallet_trigger(self) -> None:
        self._insert_wallet(WALLET_A, donated_usd=0.0)
        self._insert_tx(WALLET_A, 25.0)
        recompute_summary_for_wallet(WALLET_A, **self.kw)
        self.assertAlmostEqual(self._summary_total(), 25.0, places=2)

    def test_recompute_all_counts_linked_users(self) -> None:
        self._insert_wallet(WALLET_A, donated_usd=10.0)
        self._insert_wallet("WalletCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC", discord_id="999")
        n = recompute_all_verified_summaries(**self.kw)
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
