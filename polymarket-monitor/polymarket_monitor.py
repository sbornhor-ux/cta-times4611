"""
Polymarket Monitor - Fetches and stores prediction market data from Polymarket.

Connects to Polymarket's Gamma API, normalizes market data, and persists
snapshots to a local SQLite database with rate limiting and error handling.
"""

import enum
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

import config


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    """Configure logging to both file and console."""
    logger = logging.getLogger("polymarket_monitor")
    logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(config.LOG_FILE)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


logger = setup_logging()


# ---------------------------------------------------------------------------
# Enums & data classes
# ---------------------------------------------------------------------------

class MarketStatus(enum.Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    RESOLVED = "resolved"
    ARCHIVED = "archived"


@dataclass
class NormalizedMarket:
    platform: str
    market_id: str
    condition_id: str
    question: str
    description: str
    category: str
    status: MarketStatus
    yes_price: Optional[float]
    no_price: Optional[float]
    volume: Optional[float]
    liquidity: Optional[float]
    start_date: Optional[str]
    end_date: Optional[str]
    last_updated: str
    tags: List[str] = field(default_factory=list)
    outcomes: List[str] = field(default_factory=list)
    raw_data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["tags"] = json.dumps(self.tags)
        d["outcomes"] = json.dumps(self.outcomes)
        d["raw_data"] = json.dumps(self.raw_data)
        return d


# ---------------------------------------------------------------------------
# Rate limiter (token-bucket)
# ---------------------------------------------------------------------------

class RateLimiter:
    """Token-bucket rate limiter."""

    def __init__(self, max_tokens: int, refill_period_seconds: float = 60.0):
        self.max_tokens = max_tokens
        self.refill_period = refill_period_seconds
        self.tokens = float(max_tokens)
        self.last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        new_tokens = elapsed * (self.max_tokens / self.refill_period)
        self.tokens = min(self.max_tokens, self.tokens + new_tokens)
        self.last_refill = now

    def acquire(self) -> None:
        """Block until a token is available."""
        while True:
            self._refill()
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            sleep_time = (1.0 - self.tokens) * (
                self.refill_period / self.max_tokens
            )
            time.sleep(sleep_time)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def init_database(db_path: str) -> sqlite3.Connection:
    """Create tables if they do not exist and return a connection."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS markets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            platform        TEXT NOT NULL,
            market_id       TEXT NOT NULL,
            condition_id    TEXT,
            question        TEXT,
            description     TEXT,
            category        TEXT,
            status          TEXT,
            yes_price       REAL,
            no_price        REAL,
            volume          REAL,
            liquidity       REAL,
            start_date      TEXT,
            end_date        TEXT,
            last_updated    TEXT,
            tags            TEXT,
            outcomes        TEXT,
            raw_data        TEXT,
            snapshot_time   TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_markets_market_id
            ON markets (market_id);
        CREATE INDEX IF NOT EXISTS idx_markets_snapshot_time
            ON markets (snapshot_time);

        CREATE TABLE IF NOT EXISTS agent_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS error_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp  TEXT NOT NULL DEFAULT (datetime('now')),
            error_type TEXT,
            message    TEXT,
            details    TEXT
        );
        """
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Polymarket Monitor
# ---------------------------------------------------------------------------

class PolymarketMonitor:
    """Fetches, normalizes and stores Polymarket data."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or config.DATABASE_PATH
        self.conn = init_database(self.db_path)
        self.limiter = RateLimiter(
            max_tokens=config.MAX_CALLS_PER_MINUTE, refill_period_seconds=60.0
        )
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.consecutive_errors = 0
        self.total_polls = 0
        self.total_markets_saved = 0
        self._load_state()
        logger.info("PolymarketMonitor initialised (db=%s)", self.db_path)

    # -- state persistence --------------------------------------------------

    def _load_state(self) -> None:
        cur = self.conn.execute(
            "SELECT key, value FROM agent_state"
        )
        for key, value in cur.fetchall():
            if key == "total_polls":
                self.total_polls = int(value)
            elif key == "total_markets_saved":
                self.total_markets_saved = int(value)

    def _save_state(self) -> None:
        for key, value in [
            ("total_polls", str(self.total_polls)),
            ("total_markets_saved", str(self.total_markets_saved)),
            ("last_poll_time", datetime.now(timezone.utc).isoformat()),
        ]:
            self.conn.execute(
                "INSERT OR REPLACE INTO agent_state (key, value) VALUES (?, ?)",
                (key, value),
            )
        self.conn.commit()

    # -- API interaction -----------------------------------------------------

    def _fetch_markets(self, offset: int = 0) -> List[Dict[str, Any]]:
        """Fetch a batch of markets from the Gamma API."""
        self.limiter.acquire()
        url = f"{config.GAMMA_API_BASE}/markets"
        params = {
            "limit": config.MARKETS_BATCH_SIZE,
            "offset": offset,
            "active": "true",
        }
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _fetch_all_markets(self) -> List[Dict[str, Any]]:
        """Page through the API to retrieve all active markets."""
        all_markets: List[Dict[str, Any]] = []
        offset = 0
        while True:
            batch = self._fetch_markets(offset=offset)
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < config.MARKETS_BATCH_SIZE:
                break
            offset += len(batch)
        return all_markets

    # -- normalisation -------------------------------------------------------

    @staticmethod
    def _parse_status(raw: Dict[str, Any]) -> MarketStatus:
        if raw.get("archived", False):
            return MarketStatus.ARCHIVED
        if raw.get("resolved", False):
            return MarketStatus.RESOLVED
        if raw.get("closed", False):
            return MarketStatus.CLOSED
        if raw.get("active", True):
            return MarketStatus.ACTIVE
        return MarketStatus.ACTIVE

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    def _normalize(self, raw: Dict[str, Any]) -> NormalizedMarket:
        outcomes_raw = raw.get("outcomes") or []
        if isinstance(outcomes_raw, str):
            try:
                outcomes_raw = json.loads(outcomes_raw)
            except json.JSONDecodeError:
                outcomes_raw = [outcomes_raw]

        tags_raw = raw.get("tags") or []
        if isinstance(tags_raw, str):
            try:
                tags_raw = json.loads(tags_raw)
            except json.JSONDecodeError:
                tags_raw = [tags_raw]

        tokens = raw.get("tokens") or []
        yes_price = None
        no_price = None
        for token in tokens:
            outcome = (token.get("outcome") or "").lower()
            if outcome == "yes":
                yes_price = self._safe_float(token.get("price"))
            elif outcome == "no":
                no_price = self._safe_float(token.get("price"))

        if yes_price is None:
            yes_price = self._safe_float(raw.get("outcomePrices", [None])[0] if raw.get("outcomePrices") else None)
        if no_price is None:
            prices = raw.get("outcomePrices") or []
            no_price = self._safe_float(prices[1]) if len(prices) > 1 else None

        return NormalizedMarket(
            platform="polymarket",
            market_id=str(raw.get("id", "")),
            condition_id=raw.get("conditionId", ""),
            question=raw.get("question", ""),
            description=raw.get("description", ""),
            category=raw.get("category", "") or "",
            status=self._parse_status(raw),
            yes_price=yes_price,
            no_price=no_price,
            volume=self._safe_float(raw.get("volume")),
            liquidity=self._safe_float(raw.get("liquidity")),
            start_date=raw.get("startDate"),
            end_date=raw.get("endDate"),
            last_updated=raw.get("updatedAt", datetime.now(timezone.utc).isoformat()),
            tags=tags_raw,
            outcomes=outcomes_raw,
            raw_data=raw,
        )

    # -- persistence ---------------------------------------------------------

    def _save_markets(self, markets: List[NormalizedMarket]) -> int:
        """Insert market snapshots into the database. Returns count saved."""
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for m in markets:
            d = m.to_dict()
            d["snapshot_time"] = now
            rows.append(d)

        self.conn.executemany(
            """
            INSERT INTO markets (
                platform, market_id, condition_id, question, description,
                category, status, yes_price, no_price, volume, liquidity,
                start_date, end_date, last_updated, tags, outcomes,
                raw_data, snapshot_time
            ) VALUES (
                :platform, :market_id, :condition_id, :question, :description,
                :category, :status, :yes_price, :no_price, :volume, :liquidity,
                :start_date, :end_date, :last_updated, :tags, :outcomes,
                :raw_data, :snapshot_time
            )
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    # -- error logging -------------------------------------------------------

    def _log_error(self, error_type: str, message: str, details: str = "") -> None:
        self.conn.execute(
            "INSERT INTO error_log (error_type, message, details) VALUES (?, ?, ?)",
            (error_type, message, details),
        )
        self.conn.commit()

    # -- backoff helper ------------------------------------------------------

    def _backoff_delay(self) -> float:
        delay = min(
            2 ** self.consecutive_errors,
            config.MAX_BACKOFF_SECONDS,
        )
        return delay

    # -- public API ----------------------------------------------------------

    def poll_once(self) -> int:
        """Run a single poll cycle. Returns number of markets saved."""
        logger.info("Starting poll cycle %d", self.total_polls + 1)
        try:
            raw_markets = self._fetch_all_markets()
            logger.info("Fetched %d raw markets from API", len(raw_markets))

            normalized = [self._normalize(m) for m in raw_markets]
            count = self._save_markets(normalized)

            self.total_polls += 1
            self.total_markets_saved += count
            self.consecutive_errors = 0
            self._save_state()

            logger.info(
                "Poll cycle complete: %d markets saved (lifetime: %d)",
                count,
                self.total_markets_saved,
            )
            return count

        except requests.RequestException as exc:
            self.consecutive_errors += 1
            delay = self._backoff_delay()
            logger.error(
                "API error (consecutive=%d, backoff=%.1fs): %s",
                self.consecutive_errors,
                delay,
                exc,
            )
            self._log_error("request", str(exc))

            if self.consecutive_errors >= config.MAX_CONSECUTIVE_ERRORS:
                logger.critical(
                    "Reached %d consecutive errors — stopping.",
                    self.consecutive_errors,
                )
                raise

            time.sleep(delay)
            return 0

        except Exception as exc:
            self.consecutive_errors += 1
            logger.exception("Unexpected error during poll: %s", exc)
            self._log_error("unexpected", str(exc))
            raise

    def run(self, max_iterations: Optional[int] = None) -> None:
        """Run the monitor loop. Loops forever unless *max_iterations* is set."""
        iteration = 0
        logger.info(
            "Monitor starting (max_iterations=%s, interval=%ds)",
            max_iterations,
            config.POLL_INTERVAL_SECONDS,
        )
        try:
            while max_iterations is None or iteration < max_iterations:
                self.poll_once()
                iteration += 1
                if max_iterations is None or iteration < max_iterations:
                    logger.debug(
                        "Sleeping %ds before next poll",
                        config.POLL_INTERVAL_SECONDS,
                    )
                    time.sleep(config.POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logger.info("Monitor stopped by user")
        finally:
            self._save_state()
            logger.info("Monitor shut down after %d iterations", iteration)

    def get_status(self) -> Dict[str, Any]:
        """Return a summary of the monitor's current state."""
        cur = self.conn.execute("SELECT COUNT(*) FROM markets")
        total_rows = cur.fetchone()[0]

        cur = self.conn.execute("SELECT COUNT(*) FROM error_log")
        total_errors = cur.fetchone()[0]

        cur = self.conn.execute(
            "SELECT value FROM agent_state WHERE key = 'last_poll_time'"
        )
        row = cur.fetchone()
        last_poll = row[0] if row else None

        return {
            "total_polls": self.total_polls,
            "total_markets_saved": self.total_markets_saved,
            "total_db_rows": total_rows,
            "total_errors": total_errors,
            "consecutive_errors": self.consecutive_errors,
            "last_poll_time": last_poll,
        }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    monitor = PolymarketMonitor()
    try:
        monitor.run(max_iterations=5)
    finally:
        status = monitor.get_status()
        logger.info("Final status: %s", json.dumps(status, indent=2))
