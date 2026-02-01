"""
Movement Analyzer - Detects significant price and volume movements in prediction markets.

Analyzes market snapshots stored in SQLite, detects odds swings and capital flows,
classifies severity, and sends daily HTML email alerts.
"""

import argparse
import json
import logging
import smtplib
import sqlite3
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

import pytz
import schedule

import analyzer_config as cfg


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    logger = logging.getLogger("movement_analyzer")
    logger.setLevel(getattr(logging, cfg.LOG_LEVEL, logging.INFO))

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(cfg.LOG_FILE)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


logger = setup_logging()

CST = pytz.timezone("US/Central")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MarketMovement:
    market_id: str
    question: str
    category: str
    movement_type: str          # "odds_swing" or "capital_flow"
    severity: str               # "high", "medium", "low"
    old_price: Optional[float]
    new_price: Optional[float]
    price_change_pct: Optional[float]
    time_period_hours: float
    volume_change: Optional[float]
    volume_change_pct: Optional[float]
    detected_at: str
    movement_start: str
    movement_end: str
    hours_to_close: Optional[float]
    current_volume: Optional[float]
    current_liquidity: Optional[float]
    explanation: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------

def classify_odds_severity(price_change_pct: float) -> str:
    pct = abs(price_change_pct)
    if pct > 0.50:
        return "high"
    if pct > 0.40:
        return "medium"
    return "low"


def classify_volume_severity(volume_change_pct: float) -> str:
    pct = abs(volume_change_pct)
    if pct > 1.00:
        return "high"
    if pct > 0.75:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def init_analyzer_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id     TEXT NOT NULL,
            question      TEXT,
            category      TEXT,
            yes_price     REAL,
            no_price      REAL,
            volume        REAL,
            liquidity     REAL,
            end_date      TEXT,
            snapshot_time TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_market_time
            ON market_snapshots (market_id, snapshot_time);

        CREATE TABLE IF NOT EXISTS detected_movements (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id       TEXT NOT NULL,
            question        TEXT,
            category        TEXT,
            movement_type   TEXT NOT NULL,
            severity        TEXT NOT NULL,
            old_price       REAL,
            new_price       REAL,
            price_change_pct REAL,
            time_period_hours REAL,
            volume_change   REAL,
            volume_change_pct REAL,
            detected_at     TEXT NOT NULL,
            movement_start  TEXT,
            movement_end    TEXT,
            hours_to_close  REAL,
            current_volume  REAL,
            current_liquidity REAL,
            explanation     TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS alert_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
            recipient   TEXT NOT NULL,
            subject     TEXT,
            num_movements INTEGER,
            delivery    TEXT NOT NULL DEFAULT 'pending',
            details     TEXT
        );
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Movement Analyzer
# ---------------------------------------------------------------------------

class MovementAnalyzer:
    """Detects and reports significant market movements."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or cfg.DATABASE_PATH
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        init_analyzer_tables(self.conn)
        logger.info("MovementAnalyzer initialised (db=%s)", self.db_path)

    # -- snapshots -----------------------------------------------------------

    def take_snapshot(self) -> int:
        """Copy the latest market data into market_snapshots. Returns count."""
        cur = self.conn.execute(
            """
            SELECT market_id, question, category, yes_price, no_price,
                   volume, liquidity, end_date
            FROM markets
            WHERE id IN (
                SELECT MAX(id) FROM markets GROUP BY market_id
            )
            """
        )
        rows = cur.fetchall()
        now = datetime.now(timezone.utc).isoformat()

        self.conn.executemany(
            """
            INSERT INTO market_snapshots
                (market_id, question, category, yes_price, no_price,
                 volume, liquidity, end_date, snapshot_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["market_id"], r["question"], r["category"],
                    r["yes_price"], r["no_price"], r["volume"],
                    r["liquidity"], r["end_date"], now,
                )
                for r in rows
            ],
        )
        self.conn.commit()
        logger.info("Snapshot taken: %d markets", len(rows))
        return len(rows)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _hours_until_close(end_date: Optional[str]) -> Optional[float]:
        if not end_date:
            return None
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            delta = end_dt - datetime.now(timezone.utc)
            return delta.total_seconds() / 3600.0
        except (ValueError, TypeError):
            return None

    def _should_ignore(self, end_date: Optional[str]) -> bool:
        hours = self._hours_until_close(end_date)
        if hours is not None and 0 < hours < cfg.IGNORE_CLOSE_HOURS:
            return True
        return False

    # -- detection -----------------------------------------------------------

    def detect_odds_swings(self, window_hours: Optional[float] = None) -> List[MarketMovement]:
        """Find markets where yes_price changed > threshold within the window."""
        window = window_hours or cfg.ODDS_SWING_WINDOW_HOURS
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window)).isoformat()
        now_iso = datetime.now(timezone.utc).isoformat()

        cur = self.conn.execute(
            """
            SELECT s1.market_id, s1.question, s1.category,
                   s1.yes_price AS old_price, s2.yes_price AS new_price,
                   s2.volume AS current_volume, s2.liquidity AS current_liquidity,
                   s1.snapshot_time AS start_time, s2.snapshot_time AS end_time,
                   s2.end_date
            FROM market_snapshots s1
            JOIN market_snapshots s2
              ON s1.market_id = s2.market_id
            WHERE s1.snapshot_time >= ?
              AND s2.id = (
                  SELECT MAX(id) FROM market_snapshots
                  WHERE market_id = s1.market_id
              )
              AND s1.id = (
                  SELECT MIN(id) FROM market_snapshots
                  WHERE market_id = s1.market_id AND snapshot_time >= ?
              )
              AND s1.yes_price IS NOT NULL
              AND s2.yes_price IS NOT NULL
            """,
            (cutoff, cutoff),
        )

        movements: List[MarketMovement] = []
        for row in cur.fetchall():
            if self._should_ignore(row["end_date"]):
                continue

            old_p = row["old_price"]
            new_p = row["new_price"]
            if old_p == 0:
                continue

            change_pct = (new_p - old_p) / old_p
            if abs(change_pct) < cfg.ODDS_SWING_THRESHOLD:
                continue

            start_dt = datetime.fromisoformat(row["start_time"].replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(row["end_time"].replace("Z", "+00:00"))
            period_hours = (end_dt - start_dt).total_seconds() / 3600.0

            severity = classify_odds_severity(change_pct)
            direction = "up" if change_pct > 0 else "down"

            movements.append(
                MarketMovement(
                    market_id=row["market_id"],
                    question=row["question"] or "",
                    category=row["category"] or "",
                    movement_type="odds_swing",
                    severity=severity,
                    old_price=old_p,
                    new_price=new_p,
                    price_change_pct=round(change_pct, 4),
                    time_period_hours=round(period_hours, 2),
                    volume_change=None,
                    volume_change_pct=None,
                    detected_at=now_iso,
                    movement_start=row["start_time"],
                    movement_end=row["end_time"],
                    hours_to_close=self._hours_until_close(row["end_date"]),
                    current_volume=row["current_volume"],
                    current_liquidity=row["current_liquidity"],
                    explanation=(
                        f"Yes price moved {direction} by {abs(change_pct)*100:.1f}% "
                        f"(from {old_p:.2f} to {new_p:.2f}) "
                        f"over {period_hours:.1f} hours"
                    ),
                )
            )

        logger.info("Detected %d odds swings", len(movements))
        return movements

    def detect_capital_flows(self, window_hours: Optional[float] = None) -> List[MarketMovement]:
        """Find markets where volume increased > threshold within the window."""
        window = window_hours or cfg.CAPITAL_FLOW_WINDOW_HOURS
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window)).isoformat()
        now_iso = datetime.now(timezone.utc).isoformat()

        cur = self.conn.execute(
            """
            SELECT s1.market_id, s1.question, s1.category,
                   s1.volume AS old_volume, s2.volume AS new_volume,
                   s1.yes_price AS old_price, s2.yes_price AS new_price,
                   s2.liquidity AS current_liquidity,
                   s1.snapshot_time AS start_time, s2.snapshot_time AS end_time,
                   s2.end_date
            FROM market_snapshots s1
            JOIN market_snapshots s2
              ON s1.market_id = s2.market_id
            WHERE s1.snapshot_time >= ?
              AND s2.id = (
                  SELECT MAX(id) FROM market_snapshots
                  WHERE market_id = s1.market_id
              )
              AND s1.id = (
                  SELECT MIN(id) FROM market_snapshots
                  WHERE market_id = s1.market_id AND snapshot_time >= ?
              )
              AND s1.volume IS NOT NULL
              AND s2.volume IS NOT NULL
            """,
            (cutoff, cutoff),
        )

        movements: List[MarketMovement] = []
        for row in cur.fetchall():
            if self._should_ignore(row["end_date"]):
                continue

            old_vol = row["old_volume"]
            new_vol = row["new_volume"]
            if old_vol is None or old_vol == 0:
                continue

            vol_change = new_vol - old_vol
            vol_change_pct = vol_change / old_vol

            if vol_change_pct < cfg.CAPITAL_FLOW_THRESHOLD_PCT:
                continue
            if vol_change < cfg.CAPITAL_FLOW_MIN_DOLLARS:
                continue

            start_dt = datetime.fromisoformat(row["start_time"].replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(row["end_time"].replace("Z", "+00:00"))
            period_hours = (end_dt - start_dt).total_seconds() / 3600.0

            severity = classify_volume_severity(vol_change_pct)

            movements.append(
                MarketMovement(
                    market_id=row["market_id"],
                    question=row["question"] or "",
                    category=row["category"] or "",
                    movement_type="capital_flow",
                    severity=severity,
                    old_price=row["old_price"],
                    new_price=row["new_price"],
                    price_change_pct=None,
                    time_period_hours=round(period_hours, 2),
                    volume_change=round(vol_change, 2),
                    volume_change_pct=round(vol_change_pct, 4),
                    detected_at=now_iso,
                    movement_start=row["start_time"],
                    movement_end=row["end_time"],
                    hours_to_close=self._hours_until_close(row["end_date"]),
                    current_volume=new_vol,
                    current_liquidity=row["current_liquidity"],
                    explanation=(
                        f"Volume surged by {vol_change_pct*100:.1f}% "
                        f"(+${vol_change:,.0f}) over {period_hours:.1f} hours"
                    ),
                )
            )

        logger.info("Detected %d capital flows", len(movements))
        return movements

    # -- analysis entry point ------------------------------------------------

    def analyze_last_24_hours(self) -> List[MarketMovement]:
        """Run all detection methods and return combined movements."""
        odds = self.detect_odds_swings()
        flows = self.detect_capital_flows()
        all_movements = odds + flows

        # Persist detected movements
        for m in all_movements:
            d = m.to_dict()
            self.conn.execute(
                """
                INSERT INTO detected_movements (
                    market_id, question, category, movement_type, severity,
                    old_price, new_price, price_change_pct, time_period_hours,
                    volume_change, volume_change_pct, detected_at,
                    movement_start, movement_end, hours_to_close,
                    current_volume, current_liquidity, explanation
                ) VALUES (
                    :market_id, :question, :category, :movement_type, :severity,
                    :old_price, :new_price, :price_change_pct, :time_period_hours,
                    :volume_change, :volume_change_pct, :detected_at,
                    :movement_start, :movement_end, :hours_to_close,
                    :current_volume, :current_liquidity, :explanation
                )
                """,
                d,
            )
        self.conn.commit()

        # Sort by severity
        severity_order = {"high": 0, "medium": 1, "low": 2}
        all_movements.sort(key=lambda m: severity_order.get(m.severity, 3))

        logger.info("Analysis complete: %d total movements detected", len(all_movements))
        return all_movements

    # -- email report --------------------------------------------------------

    def generate_email_report(self, movements: List[MarketMovement]) -> str:
        """Create an HTML email report from the detected movements."""
        now_cst = datetime.now(CST).strftime("%B %d, %Y %I:%M %p CST")

        severity_colors = {
            "high": "#dc3545",
            "medium": "#fd7e14",
            "low": "#ffc107",
        }

        type_labels = {
            "odds_swing": "Odds Swing",
            "capital_flow": "Capital Flow",
        }

        # Build movement rows
        movement_rows = ""
        for m in movements:
            color = severity_colors.get(m.severity, "#6c757d")
            type_label = type_labels.get(m.movement_type, m.movement_type)

            detail = ""
            if m.movement_type == "odds_swing" and m.price_change_pct is not None:
                direction = "&#9650;" if m.price_change_pct > 0 else "&#9660;"
                detail = (
                    f"{direction} {abs(m.price_change_pct)*100:.1f}% "
                    f"({m.old_price:.2f} &rarr; {m.new_price:.2f})"
                )
            elif m.movement_type == "capital_flow" and m.volume_change is not None:
                detail = (
                    f"+${m.volume_change:,.0f} "
                    f"({m.volume_change_pct*100:.1f}% increase)"
                )

            hours_close_str = (
                f"{m.hours_to_close:.0f}h" if m.hours_to_close is not None else "N/A"
            )

            movement_rows += f"""
            <tr>
                <td style="padding:12px;border-bottom:1px solid #dee2e6;">
                    <span style="display:inline-block;padding:2px 8px;border-radius:4px;
                        background-color:{color};color:#fff;font-size:12px;font-weight:600;
                        text-transform:uppercase;">{m.severity}</span>
                </td>
                <td style="padding:12px;border-bottom:1px solid #dee2e6;">
                    <span style="background:#e9ecef;padding:2px 6px;border-radius:3px;
                        font-size:12px;">{type_label}</span>
                </td>
                <td style="padding:12px;border-bottom:1px solid #dee2e6;max-width:300px;">
                    <strong>{m.question[:120]}</strong>
                    <br><span style="color:#6c757d;font-size:12px;">{m.category}</span>
                </td>
                <td style="padding:12px;border-bottom:1px solid #dee2e6;">{detail}</td>
                <td style="padding:12px;border-bottom:1px solid #dee2e6;text-align:center;">
                    {m.time_period_hours:.1f}h
                </td>
                <td style="padding:12px;border-bottom:1px solid #dee2e6;text-align:center;">
                    {hours_close_str}
                </td>
            </tr>
            """

        # Summary counts
        high_count = sum(1 for m in movements if m.severity == "high")
        medium_count = sum(1 for m in movements if m.severity == "medium")
        low_count = sum(1 for m in movements if m.severity == "low")
        odds_count = sum(1 for m in movements if m.movement_type == "odds_swing")
        flow_count = sum(1 for m in movements if m.movement_type == "capital_flow")

        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    margin:0;padding:0;background:#f8f9fa;">
<div style="max-width:800px;margin:20px auto;background:#fff;border-radius:8px;
    box-shadow:0 2px 4px rgba(0,0,0,0.1);overflow:hidden;">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#1a1a2e,#16213e);padding:24px 32px;
        color:#fff;">
        <h1 style="margin:0 0 4px 0;font-size:22px;">Prediction Market Movement Alert</h1>
        <p style="margin:0;opacity:0.8;font-size:14px;">{now_cst}</p>
    </div>

    <!-- Summary -->
    <div style="padding:20px 32px;background:#f1f3f5;border-bottom:1px solid #dee2e6;">
        <table style="width:100%;border-collapse:collapse;">
        <tr>
            <td style="text-align:center;padding:8px;">
                <div style="font-size:28px;font-weight:700;color:#212529;">{len(movements)}</div>
                <div style="font-size:12px;color:#6c757d;text-transform:uppercase;">Total</div>
            </td>
            <td style="text-align:center;padding:8px;">
                <div style="font-size:28px;font-weight:700;color:#dc3545;">{high_count}</div>
                <div style="font-size:12px;color:#6c757d;text-transform:uppercase;">High</div>
            </td>
            <td style="text-align:center;padding:8px;">
                <div style="font-size:28px;font-weight:700;color:#fd7e14;">{medium_count}</div>
                <div style="font-size:12px;color:#6c757d;text-transform:uppercase;">Medium</div>
            </td>
            <td style="text-align:center;padding:8px;">
                <div style="font-size:28px;font-weight:700;color:#ffc107;">{low_count}</div>
                <div style="font-size:12px;color:#6c757d;text-transform:uppercase;">Low</div>
            </td>
            <td style="text-align:center;padding:8px;">
                <div style="font-size:28px;font-weight:700;color:#0d6efd;">{odds_count}</div>
                <div style="font-size:12px;color:#6c757d;text-transform:uppercase;">Odds Swings</div>
            </td>
            <td style="text-align:center;padding:8px;">
                <div style="font-size:28px;font-weight:700;color:#198754;">{flow_count}</div>
                <div style="font-size:12px;color:#6c757d;text-transform:uppercase;">Capital Flows</div>
            </td>
        </tr>
        </table>
    </div>

    <!-- Movements table -->
    <div style="padding:20px 32px;">
        {"<p style='color:#6c757d;text-align:center;padding:40px 0;'>No significant movements detected in the last 24 hours.</p>" if not movements else ""}
        {"<table style='width:100%;border-collapse:collapse;font-size:14px;'><thead><tr style='background:#f8f9fa;'><th style='padding:10px 12px;text-align:left;border-bottom:2px solid #dee2e6;'>Severity</th><th style='padding:10px 12px;text-align:left;border-bottom:2px solid #dee2e6;'>Type</th><th style='padding:10px 12px;text-align:left;border-bottom:2px solid #dee2e6;'>Market</th><th style='padding:10px 12px;text-align:left;border-bottom:2px solid #dee2e6;'>Change</th><th style='padding:10px 12px;text-align:center;border-bottom:2px solid #dee2e6;'>Period</th><th style='padding:10px 12px;text-align:center;border-bottom:2px solid #dee2e6;'>To Close</th></tr></thead><tbody>" + movement_rows + "</tbody></table>" if movements else ""}
    </div>

    <!-- Footer -->
    <div style="padding:16px 32px;background:#f8f9fa;border-top:1px solid #dee2e6;
        text-align:center;font-size:12px;color:#6c757d;">
        Polymarket Movement Analyzer &bull; Data sourced from Polymarket Gamma API
    </div>
</div>
</body>
</html>"""
        return html

    # -- email sending -------------------------------------------------------

    def send_email_alert(self, subject: str, html_body: str) -> bool:
        """Send HTML email via SMTP. Falls back to saving to file if no credentials."""
        recipient = cfg.ALERT_EMAIL

        if not cfg.SENDER_EMAIL or not cfg.SENDER_PASSWORD:
            fallback_path = f"alert_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.html"
            with open(fallback_path, "w", encoding="utf-8") as f:
                f.write(html_body)
            logger.warning(
                "No SMTP credentials configured. Report saved to %s", fallback_path
            )
            self.conn.execute(
                "INSERT INTO alert_history (recipient, subject, num_movements, delivery, details) "
                "VALUES (?, ?, 0, 'file', ?)",
                (recipient, subject, f"Saved to {fallback_path}"),
            )
            self.conn.commit()
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = cfg.SENDER_EMAIL
        msg["To"] = recipient
        msg.attach(MIMEText(html_body, "html"))

        try:
            with smtplib.SMTP(cfg.SMTP_SERVER, cfg.SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(cfg.SENDER_EMAIL, cfg.SENDER_PASSWORD)
                server.sendmail(cfg.SENDER_EMAIL, [recipient], msg.as_string())

            logger.info("Email alert sent to %s", recipient)
            self.conn.execute(
                "INSERT INTO alert_history (recipient, subject, num_movements, delivery) "
                "VALUES (?, ?, 0, 'sent')",
                (recipient, subject),
            )
            self.conn.commit()
            return True

        except Exception as exc:
            logger.error("Failed to send email: %s", exc)
            fallback_path = f"alert_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.html"
            with open(fallback_path, "w", encoding="utf-8") as f:
                f.write(html_body)
            logger.info("Report saved to %s as fallback", fallback_path)
            self.conn.execute(
                "INSERT INTO alert_history (recipient, subject, num_movements, delivery, details) "
                "VALUES (?, ?, 0, 'failed', ?)",
                (recipient, subject, str(exc)),
            )
            self.conn.commit()
            return False

    # -- daily alert ---------------------------------------------------------

    def send_daily_alert(self) -> None:
        """Generate analysis and send the daily summary email."""
        logger.info("Running daily alert")
        movements = self.analyze_last_24_hours()
        now_cst = datetime.now(CST).strftime("%Y-%m-%d")
        subject = (
            f"[Polymarket] {len(movements)} market movements detected — {now_cst}"
        )
        html = self.generate_email_report(movements)
        self.send_email_alert(subject, html)

    # -- continuous mode -----------------------------------------------------

    def run_continuous(self) -> None:
        """Take snapshots hourly, send daily alert at configured time (CST)."""
        logger.info(
            "Starting continuous mode: snapshots every %dh, alert at %s CST",
            cfg.SNAPSHOT_INTERVAL_HOURS,
            cfg.ALERT_TIME_CST,
        )

        # Take an initial snapshot
        self.take_snapshot()

        # Schedule snapshot
        schedule.every(cfg.SNAPSHOT_INTERVAL_HOURS).hours.do(self.take_snapshot)

        # Schedule daily alert at configured CST time
        # schedule library uses local time; convert CST target to local
        schedule.every().day.at(cfg.ALERT_TIME_CST).do(self.send_daily_alert)

        try:
            while True:
                schedule.run_pending()
                time.sleep(30)
        except KeyboardInterrupt:
            logger.info("Continuous mode stopped by user")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prediction Market Movement Analyzer"
    )
    parser.add_argument(
        "mode",
        choices=["analyze", "continuous", "test"],
        help="Run mode: analyze (one-shot), continuous (scheduled), test (demo run)",
    )
    parser.add_argument(
        "--db",
        default=cfg.DATABASE_PATH,
        help="Path to SQLite database",
    )
    args = parser.parse_args()

    analyzer = MovementAnalyzer(db_path=args.db)

    if args.mode == "analyze":
        movements = analyzer.analyze_last_24_hours()
        html = analyzer.generate_email_report(movements)
        now_cst = datetime.now(CST).strftime("%Y-%m-%d")
        subject = (
            f"[Polymarket] {len(movements)} market movements detected — {now_cst}"
        )
        analyzer.send_email_alert(subject, html)
        print(f"Analysis complete: {len(movements)} movements detected")

    elif args.mode == "continuous":
        analyzer.run_continuous()

    elif args.mode == "test":
        logger.info("=== TEST MODE ===")
        count = analyzer.take_snapshot()
        print(f"Snapshot taken: {count} markets")

        movements = analyzer.analyze_last_24_hours()
        print(f"Movements detected: {len(movements)}")
        for m in movements:
            print(f"  [{m.severity.upper()}] {m.movement_type}: {m.explanation}")

        html = analyzer.generate_email_report(movements)
        test_file = "test_alert.html"
        with open(test_file, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Test report saved to {test_file}")


if __name__ == "__main__":
    main()
