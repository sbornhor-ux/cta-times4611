"""
Integrated Monitoring System - Combines Polymarket data collection with movement analysis.

Runs the PolymarketMonitor and MovementAnalyzer in parallel threads, providing
continuous market surveillance with periodic status updates and daily alerts.
"""

import argparse
import json
import logging
import threading
import time
from datetime import datetime, timezone

from polymarket_monitor import PolymarketMonitor
from movement_analyzer import MovementAnalyzer

import analyzer_config as acfg
import config


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("integrated_system")


# ---------------------------------------------------------------------------
# Integrated system
# ---------------------------------------------------------------------------

class IntegratedMonitoringSystem:
    """Runs market data collection and movement analysis in parallel."""

    def __init__(self, db_path: str = config.DATABASE_PATH):
        self.db_path = db_path
        self.monitor = PolymarketMonitor(db_path=db_path)
        self.analyzer = MovementAnalyzer(db_path=db_path)
        self._stop_event = threading.Event()

    # -- threads -------------------------------------------------------------

    def run_monitor_loop(self) -> None:
        """Continuously poll Polymarket for new data."""
        logger.info("Monitor loop started")
        while not self._stop_event.is_set():
            try:
                self.monitor.poll_once()
            except Exception as exc:
                logger.error("Monitor poll error: %s", exc)
            self._stop_event.wait(timeout=config.POLL_INTERVAL_SECONDS)
        logger.info("Monitor loop stopped")

    def run_analyzer_loop(self) -> None:
        """Continuously take snapshots and run scheduled analysis."""
        logger.info("Analyzer loop started")
        # Initial snapshot
        try:
            self.analyzer.take_snapshot()
        except Exception as exc:
            logger.error("Initial snapshot error: %s", exc)

        snapshot_interval = acfg.SNAPSHOT_INTERVAL_HOURS * 3600
        last_alert_date = None

        while not self._stop_event.is_set():
            try:
                self.analyzer.take_snapshot()
                logger.info("Snapshot taken")
            except Exception as exc:
                logger.error("Snapshot error: %s", exc)

            # Check if it's time for the daily alert
            try:
                from pytz import timezone as pytz_tz
                cst = pytz_tz("US/Central")
                now_cst = datetime.now(cst)
                alert_hour, alert_minute = (
                    int(x) for x in acfg.ALERT_TIME_CST.split(":")
                )
                today_str = now_cst.strftime("%Y-%m-%d")

                if (
                    now_cst.hour == alert_hour
                    and now_cst.minute < (alert_minute + 30)
                    and last_alert_date != today_str
                ):
                    logger.info("Triggering daily alert")
                    self.analyzer.send_daily_alert()
                    last_alert_date = today_str
            except Exception as exc:
                logger.error("Daily alert check error: %s", exc)

            self._stop_event.wait(timeout=snapshot_interval)

        logger.info("Analyzer loop stopped")

    # -- status --------------------------------------------------------------

    def _print_status(self) -> None:
        status = self.monitor.get_status()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print()
        print("=" * 60)
        print(f"  STATUS UPDATE — {now}")
        print("=" * 60)
        print(f"  Total polls completed : {status['total_polls']}")
        print(f"  Total markets saved   : {status['total_markets_saved']}")
        print(f"  Database rows         : {status['total_db_rows']}")
        print(f"  Errors logged         : {status['total_errors']}")
        print(f"  Consecutive errors    : {status['consecutive_errors']}")
        print(f"  Last poll             : {status['last_poll_time'] or 'N/A'}")
        print("=" * 60)
        print()

    # -- integrated run ------------------------------------------------------

    def run_integrated(self) -> None:
        """Run monitor and analyzer in parallel threads."""
        print()
        print("=" * 60)
        print("  POLYMARKET INTEGRATED MONITORING SYSTEM")
        print("=" * 60)
        print()
        print(f"  Database       : {self.db_path}")
        print(f"  Poll interval  : {config.POLL_INTERVAL_SECONDS}s")
        print(f"  Snapshot every : {acfg.SNAPSHOT_INTERVAL_HOURS}h")
        print(f"  Daily alert at : {acfg.ALERT_TIME_CST} CST")
        print(f"  Alert email    : {acfg.ALERT_EMAIL}")
        print(f"  API base       : {config.GAMMA_API_BASE}")
        print()
        print("  Starting threads...")
        print("  Press Ctrl+C to stop")
        print("=" * 60)
        print()

        monitor_thread = threading.Thread(
            target=self.run_monitor_loop, name="monitor", daemon=True
        )
        analyzer_thread = threading.Thread(
            target=self.run_analyzer_loop, name="analyzer", daemon=True
        )

        monitor_thread.start()
        analyzer_thread.start()
        logger.info("Both threads started")

        try:
            status_interval = 3600  # 1 hour
            elapsed = 0
            tick = 10  # check stop every 10s
            while True:
                time.sleep(tick)
                elapsed += tick
                if elapsed >= status_interval:
                    self._print_status()
                    elapsed = 0
        except KeyboardInterrupt:
            print()
            print("Shutting down gracefully...")
            self._stop_event.set()

        monitor_thread.join(timeout=15)
        analyzer_thread.join(timeout=15)

        self._print_status()
        print("System stopped.")


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

def quick_test(db_path: str = config.DATABASE_PATH) -> None:
    """Run a quick end-to-end test of the entire pipeline."""
    print()
    print("=" * 60)
    print("  QUICK TEST — Polymarket Monitoring Pipeline")
    print("=" * 60)
    print()

    # Step 1: Poll market data
    print("[1/5] Polling market data from Polymarket API...")
    monitor = PolymarketMonitor(db_path=db_path)
    try:
        count = monitor.poll_once()
        print(f"       Fetched and saved {count} markets")
    except Exception as exc:
        print(f"       API error (expected if no network): {exc}")
        print("       Continuing with any existing data...")

    # Step 2: First snapshot
    print()
    print("[2/5] Taking first market snapshot...")
    analyzer = MovementAnalyzer(db_path=db_path)
    snap1 = analyzer.take_snapshot()
    print(f"       Snapshot 1: {snap1} markets captured")

    # Step 3: Wait and take second snapshot
    print()
    print("[3/5] Waiting 5 seconds before second snapshot...")
    time.sleep(5)
    snap2 = analyzer.take_snapshot()
    print(f"       Snapshot 2: {snap2} markets captured")

    # Step 4: Analyze movements
    print()
    print("[4/5] Analyzing market movements...")
    movements = analyzer.analyze_last_24_hours()
    print(f"       Detected {len(movements)} movements")

    if movements:
        high = sum(1 for m in movements if m.severity == "high")
        medium = sum(1 for m in movements if m.severity == "medium")
        low = sum(1 for m in movements if m.severity == "low")
        odds = sum(1 for m in movements if m.movement_type == "odds_swing")
        flows = sum(1 for m in movements if m.movement_type == "capital_flow")

        print(f"       Severity  — High: {high}, Medium: {medium}, Low: {low}")
        print(f"       Types     — Odds swings: {odds}, Capital flows: {flows}")
        print()
        print("       Top movements:")
        for m in movements[:5]:
            print(f"         [{m.severity.upper():6s}] {m.explanation}")
    else:
        print("       (No significant movements — normal for short test window)")

    # Step 5: Generate report
    print()
    print("[5/5] Generating HTML report...")
    html = analyzer.generate_email_report(movements)
    report_file = "test_report.html"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"       Report saved to {report_file}")

    # Summary
    status = monitor.get_status()
    print()
    print("=" * 60)
    print("  TEST SUMMARY")
    print("=" * 60)
    print(f"  Markets polled      : {status['total_markets_saved']}")
    print(f"  Snapshots taken     : 2")
    print(f"  Movements found     : {len(movements)}")
    print(f"  Database rows       : {status['total_db_rows']}")
    print(f"  Report              : {report_file}")
    print(f"  Database            : {db_path}")
    print("=" * 60)
    print()
    print("Test complete. Open test_report.html in a browser to view the report.")
    print()


# ---------------------------------------------------------------------------
# Production mode
# ---------------------------------------------------------------------------

def production_mode(db_path: str = config.DATABASE_PATH) -> None:
    """Run the integrated system continuously."""
    system = IntegratedMonitoringSystem(db_path=db_path)
    system.run_integrated()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Polymarket Integrated Monitoring System",
    )
    parser.add_argument(
        "--mode",
        choices=["test", "production"],
        default="test",
        help="Run mode (default: test)",
    )
    parser.add_argument(
        "--email",
        default="sbornhor@uchicago.edu",
        help="Alert recipient email (default: sbornhor@uchicago.edu)",
    )
    parser.add_argument(
        "--db",
        default=config.DATABASE_PATH,
        help="Path to SQLite database",
    )
    args = parser.parse_args()

    # Override email if provided
    acfg.ALERT_EMAIL = args.email

    if args.mode == "test":
        quick_test(db_path=args.db)
    elif args.mode == "production":
        production_mode(db_path=args.db)


if __name__ == "__main__":
    main()
