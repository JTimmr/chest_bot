#!/usr/bin/env python3
"""
Plot cumulative FARTBOY held by the top N wallets (by balance), from snapshot SQLite.

X: wallet rank N (1 = largest holder only, 2 = two largest combined, ...).
Y: total FARTBOY in those top N wallets.

By default, balances at ranks 1, 3, and 4 by size are dropped (e.g. exchange hot
wallets) before ranking and cumulating.

Writes two PNGs: linear axes and log–log axes.
"""

import argparse
import os
import sqlite3
from typing import List, Set, Tuple


def load_balances(conn: sqlite3.Connection, table: str) -> List[float]:
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT amount_fartboy
        FROM {table}
        WHERE amount_fartboy > 0
        """
    )
    rows = cur.fetchall()
    amounts = [float(r[0]) for r in rows]
    amounts.sort(reverse=True)
    return amounts


def parse_skip_ranks(s: str) -> Set[int]:
    """1-based ranks by descending balance, e.g. '1,3,4' -> {1, 3, 4}."""
    ranks: Set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        r = int(part)
        if r < 1:
            raise ValueError(f"skip rank must be >= 1, got {r}")
        ranks.add(r)
    return ranks


def drop_balances_at_ranks(sorted_desc: List[float], skip_ranks_1based: Set[int]) -> List[float]:
    """Remove the Nth-largest balance for each N in skip_ranks_1based (1 = largest)."""
    if not skip_ranks_1based:
        return list(sorted_desc)
    return [
        amt
        for i, amt in enumerate(sorted_desc)
        if (i + 1) not in skip_ranks_1based
    ]


def cumulative_series(amounts_desc: List[float]) -> Tuple[List[int], List[float]]:
    xs: List[int] = []
    ys: List[float] = []
    total = 0.0
    for i, amt in enumerate(amounts_desc, start=1):
        total += amt
        xs.append(i)
        ys.append(total)
    return xs, ys


def write_plot(
    plt,
    xs: List[int],
    ys: List[float],
    out_path: str,
    *,
    loglog: bool,
    title_suffix: str,
) -> None:
    fig, ax = plt.subplots(figsize=(10, 6))
    base = "Cumulative FARTBOY held by top N wallets"
    title = f"{base}{title_suffix}"
    if loglog:
        ax.loglog(xs, ys, color="#2d6a4f", linewidth=1.5)
        ax.set_title(f"{title} (log–log)")
    else:
        ax.plot(xs, ys, color="#2d6a4f", linewidth=1.5)
        ax.set_title(title)
    ax.set_xlabel("Number of wallets (top N by balance)")
    ax.set_ylabel("Total FARTBOY (cumulative)")
    ax.grid(True, alpha=0.3, which="both" if loglog else "major")
    fig.tight_layout()

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plot cumulative FARTBOY in top N wallets from snapshot DB."
    )
    parser.add_argument(
        "--db",
        default="data/fartboy_snapshot.db",
        help="SQLite database path (same default as snapshot_fartboy_holders.py)",
    )
    parser.add_argument(
        "--table",
        default="fartboy_holders",
        help="Holders table name",
    )
    parser.add_argument(
        "--out",
        default="data/fartboy_top_holders_curve.png",
        help="Output PNG path (linear axes)",
    )
    parser.add_argument(
        "--out-loglog",
        default="data/fartboy_top_holders_curve_loglog.png",
        help="Output PNG path (log–log axes)",
    )
    parser.add_argument(
        "--skip-ranks",
        default="1,3,4",
        help=(
            "Comma-separated 1-based ranks (by balance, largest first) to omit "
            "before plotting; empty to omit none. Default skips exchange-sized slots."
        ),
    )
    args = parser.parse_args()

    try:
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise SystemExit(
            "Missing matplotlib. Install with: pip install matplotlib"
        ) from e

    conn = sqlite3.connect(args.db)
    try:
        amounts = load_balances(conn, args.table)
    finally:
        conn.close()

    if not amounts:
        raise SystemExit(f"No rows with amount_fartboy > 0 in {args.db}:{args.table}")

    try:
        skip_ranks = (
            parse_skip_ranks(args.skip_ranks)
            if args.skip_ranks.strip()
            else set()
        )
    except ValueError as e:
        raise SystemExit(f"Invalid --skip-ranks: {e}") from e

    filtered = drop_balances_at_ranks(amounts, skip_ranks)
    if not filtered:
        raise SystemExit("After --skip-ranks, no balances left to plot.")

    xs, ys = cumulative_series(filtered)

    if skip_ranks:
        shown = ", ".join(str(n) for n in sorted(skip_ranks))
        title_suffix = f" (ranks {shown} by size excluded)"
    else:
        title_suffix = ""

    write_plot(plt, xs, ys, args.out, loglog=False, title_suffix=title_suffix)
    write_plot(plt, xs, ys, args.out_loglog, loglog=True, title_suffix=title_suffix)

    print(f"Wrote {args.out} (linear)")
    print(f"Wrote {args.out_loglog} (log–log)")
    print(
        f"({len(amounts)} wallets in snapshot, {len(filtered)} used after skip-ranks)"
    )


if __name__ == "__main__":
    main()
