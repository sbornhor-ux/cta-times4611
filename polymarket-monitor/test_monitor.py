"""
Test Monitor - Utilities for testing and debugging the Polymarket monitoring system.

Provides CLI commands to poll data, query the database, analyze market statistics,
and inspect errors.
"""

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from polymarket_monitor import PolymarketMonitor
from config import DATABASE_PATH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def print_separator(title: str) -> None:
    """Print a formatted section separator."""
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)
    print()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_volume(vol: Optional[float]) -> str:
    if vol is None:
        return "N/A"
    if vol >= 1_000_000:
        return f"${vol / 1_000_000:,.2f}M"
    if vol >= 1_000:
        return f"${vol / 1_000:,.1f}K"
    return f"${vol:,.2f}"


def _fmt_price(price: Optional[float]) -> str:
    if price is None:
        return "N/A"
    return f"{price:.4f}"


def _print_market(row: sqlite3.Row, index: int) -> None:
    """Print a single market row with formatted details."""
    print(f"  {index}. {row['question'] or '(no question)'}")
    print(f"     Market ID  : {row['market_id']}")
    print(f"     Category   : {row['category'] or 'N/A'}")
    print(f"     Status     : {row['status'] or 'N/A'}")
    print(f"     Yes Price  : {_fmt_price(row['yes_price'])}")
    print(f"     No Price   : {_fmt_price(row['no_price'])}")
    print(f"     Volume     : {_fmt_volume(row['volume'])}")
    print(f"     Liquidity  : {_fmt_volume(row['liquidity'])}")
    print(f"     Updated    : {row['last_updated'] or 'N/A'}")
    print()


# ---------------------------------------------------------------------------
# Test functions
# ---------------------------------------------------------------------------

def test_single_poll() -> None:
    """Run a single poll cycle and display results."""
    print_separator("SINGLE POLL TEST")

    monitor = PolymarketMonitor(db_path=DATABASE_PATH)
    print("  Polling Polymarket API...")
    try:
        count = monitor.poll_once()
        print(f"  Successfully fetched and saved {count} markets")
    except Exception as exc:
        print(f"  Error during poll: {exc}")
        return

    print()
    status = monitor.get_status()
    print("  Monitor Status:")
    print(f"    Total polls         : {status['total_polls']}")
    print(f"    Total markets saved : {status['total_markets_saved']}")
    print(f"    Database rows       : {status['total_db_rows']}")
    print(f"    Errors logged       : {status['total_errors']}")
    print(f"    Last poll           : {status['last_poll_time'] or 'N/A'}")


def query_markets(limit: int = 5) -> None:
    """Query and display the latest markets from the database."""
    print_separator(f"LATEST {limit} MARKETS")

    conn = _connect()
    cur = conn.execute(
        """
        SELECT market_id, question, category, status, yes_price, no_price,
               volume, liquidity, last_updated
        FROM markets
        WHERE id IN (
            SELECT MAX(id) FROM markets GROUP BY market_id
        )
        ORDER BY volume DESC NULLS LAST
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("  No markets found in database.")
        print("  Run 'poll' first to fetch data.")
        return

    print(f"  Showing top {len(rows)} markets by volume:\n")
    for i, row in enumerate(rows, 1):
        _print_market(row, i)


def query_errors() -> None:
    """Display recent errors from the error_log table."""
    print_separator("RECENT ERRORS")

    conn = _connect()
    cur = conn.execute(
        """
        SELECT id, timestamp, error_type, message, details
        FROM error_log
        ORDER BY id DESC
        LIMIT 10
        """,
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("  No errors recorded.")
        return

    print(f"  Showing {len(rows)} most recent errors:\n")
    for row in rows:
        print(f"  [{row['timestamp']}] {row['error_type']}")
        print(f"    Message: {row['message']}")
        if row["details"]:
            print(f"    Details: {row['details'][:200]}")
        print()


def analyze_markets() -> None:
    """Show statistics about markets in the database."""
    print_separator("MARKET ANALYSIS")

    conn = _connect()

    # Total unique markets
    cur = conn.execute("SELECT COUNT(DISTINCT market_id) FROM markets")
    total_unique = cur.fetchone()[0]

    # Total snapshots
    cur = conn.execute("SELECT COUNT(*) FROM markets")
    total_rows = cur.fetchone()[0]

    print(f"  Total unique markets : {total_unique}")
    print(f"  Total snapshots      : {total_rows}")

    # Markets by status
    print()
    print("  Markets by Status:")
    cur = conn.execute(
        """
        SELECT status, COUNT(*) as cnt
        FROM (
            SELECT market_id, status
            FROM markets
            WHERE id IN (SELECT MAX(id) FROM markets GROUP BY market_id)
        )
        GROUP BY status
        ORDER BY cnt DESC
        """,
    )
    for row in cur.fetchall():
        print(f"    {row['status'] or 'unknown':15s} : {row['cnt']}")

    # Markets by category with volume
    print()
    print("  Markets by Category (with total volume):")
    cur = conn.execute(
        """
        SELECT category,
               COUNT(*) as cnt,
               SUM(volume) as total_volume,
               AVG(volume) as avg_volume
        FROM (
            SELECT market_id, category, volume
            FROM markets
            WHERE id IN (SELECT MAX(id) FROM markets GROUP BY market_id)
        )
        GROUP BY category
        ORDER BY total_volume DESC NULLS LAST
        LIMIT 15
        """,
    )
    rows = cur.fetchall()
    if rows:
        print(f"    {'Category':25s} {'Count':>6s} {'Total Volume':>14s} {'Avg Volume':>14s}")
        print(f"    {'-'*25} {'-'*6} {'-'*14} {'-'*14}")
        for row in rows:
            cat = (row["category"] or "uncategorized")[:25]
            total_vol = _fmt_volume(row["total_volume"])
            avg_vol = _fmt_volume(row["avg_volume"])
            print(f"    {cat:25s} {row['cnt']:6d} {total_vol:>14s} {avg_vol:>14s}")

    # Volume statistics
    print()
    print("  Volume Statistics (latest snapshot per market):")
    cur = conn.execute(
        """
        SELECT COUNT(*) as cnt,
               MIN(volume) as min_vol,
               MAX(volume) as max_vol,
               AVG(volume) as avg_vol,
               SUM(volume) as sum_vol
        FROM (
            SELECT market_id, volume
            FROM markets
            WHERE id IN (SELECT MAX(id) FROM markets GROUP BY market_id)
              AND volume IS NOT NULL
        )
        """,
    )
    row = cur.fetchone()
    if row and row["cnt"] > 0:
        print(f"    Markets with volume : {row['cnt']}")
        print(f"    Min volume          : {_fmt_volume(row['min_vol'])}")
        print(f"    Max volume          : {_fmt_volume(row['max_vol'])}")
        print(f"    Avg volume          : {_fmt_volume(row['avg_vol'])}")
        print(f"    Total volume        : {_fmt_volume(row['sum_vol'])}")
    else:
        print("    No volume data available.")

    conn.close()


def show_high_volume_markets(min_volume: float = 10000) -> None:
    """Display markets above a volume threshold."""
    print_separator(f"HIGH VOLUME MARKETS (>{_fmt_volume(min_volume)})")

    conn = _connect()
    cur = conn.execute(
        """
        SELECT market_id, question, category, status, yes_price, no_price,
               volume, liquidity, last_updated
        FROM markets
        WHERE id IN (SELECT MAX(id) FROM markets GROUP BY market_id)
          AND volume >= ?
        ORDER BY volume DESC
        """,
        (min_volume,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print(f"  No markets found with volume >= {_fmt_volume(min_volume)}.")
        print("  Try lowering the threshold or polling more data.")
        return

    print(f"  Found {len(rows)} markets:\n")
    for i, row in enumerate(rows, 1):
        _print_market(row, i)


# ---------------------------------------------------------------------------
# Full demo
# ---------------------------------------------------------------------------

def run_full_demo() -> None:
    """Run all test functions in sequence."""
    print()
    print("#" * 70)
    print("#" + " POLYMARKET MONITOR — FULL DEMO ".center(68) + "#")
    print("#" * 70)

    test_single_poll()
    query_markets(limit=5)
    analyze_markets()
    show_high_volume_markets(min_volume=10000)
    query_errors()

    print_separator("DEMO COMPLETE")
    print("  All tests finished. Review output above for details.")
    print(f"  Database: {DATABASE_PATH}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test and debug the Polymarket monitoring system",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["poll", "query", "analyze", "errors", "high-volume"],
        default=None,
        help="Command to run (default: run full demo)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of markets to display (default: 5)",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=10000,
        help="Minimum volume for high-volume filter (default: 10000)",
    )
    args = parser.parse_args()

    if args.command is None:
        run_full_demo()
    elif args.command == "poll":
        test_single_poll()
    elif args.command == "query":
        query_markets(limit=args.limit)
    elif args.command == "analyze":
        analyze_markets()
    elif args.command == "errors":
        query_errors()
    elif args.command == "high-volume":
        show_high_volume_markets(min_volume=args.min_volume)


if __name__ == "__main__":
    main()
