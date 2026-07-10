import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "stockbot.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                message_id  TEXT PRIMARY KEY,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ticker_alerts (
                symbol     TEXT NOT NULL,
                alerted_at TIMESTAMP NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ticker_alerts_symbol
                ON ticker_alerts (symbol, alerted_at);

            CREATE TABLE IF NOT EXISTS positions (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol                  TEXT NOT NULL,
                provider                TEXT NOT NULL,
                shares                  INTEGER NOT NULL,
                buy_price               REAL NOT NULL,
                buy_time                TIMESTAMP NOT NULL,
                buy_order_id            TEXT,
                trailing_stop_order_id  TEXT,
                hard_stop_order_id      TEXT,
                stop_tightened          INTEGER DEFAULT 0,
                status                  TEXT DEFAULT 'open',
                sell_price              REAL,
                sell_time               TIMESTAMP,
                pnl                     REAL,
                created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                rsi_at_entry            REAL,
                atr_at_entry            REAL,
                change_pct_at_entry     REAL,
                macd_crossover_fresh    INTEGER,
                rvol_at_entry           REAL
            );

            CREATE TABLE IF NOT EXISTS price_bars (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol    TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                open      REAL NOT NULL,
                high      REAL NOT NULL,
                low       REAL NOT NULL,
                close     REAL NOT NULL,
                volume    INTEGER NOT NULL,
                UNIQUE(symbol, timestamp)
            );

            CREATE TABLE IF NOT EXISTS wallets (
                screener_id        TEXT PRIMARY KEY,
                initial_balance    REAL NOT NULL,
                current_balance    REAL NOT NULL,
                day_start_balance  REAL NOT NULL,
                day_date           TEXT NOT NULL,
                updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # migrations for existing databases
        _migrations = [
            ("stop_tightened",        "INTEGER DEFAULT 0"),
            ("rsi_at_entry",          "REAL"),
            ("atr_at_entry",          "REAL"),
            ("change_pct_at_entry",   "REAL"),
            ("macd_crossover_fresh",  "INTEGER"),
            ("rvol_at_entry",         "REAL"),
            ("hard_stop_order_id",    "TEXT"),
        ]
        for col, typedef in _migrations:
            try:
                conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {typedef}")
            except Exception:
                pass
    logger.info("Database ready: %s", DB_PATH)


def is_email_processed(message_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_emails WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None


def mark_email_processed(message_id: str):
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_emails (message_id) VALUES (?)", (message_id,)
        )


def save_position(
    symbol: str,
    provider: str,
    shares: int,
    buy_price: float,
    buy_time: datetime,
    buy_order_id: str,
    rsi_at_entry: Optional[float] = None,
    atr_at_entry: Optional[float] = None,
    change_pct_at_entry: Optional[float] = None,
    macd_crossover_fresh: Optional[bool] = None,
    rvol_at_entry: Optional[float] = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO positions
               (symbol, provider, shares, buy_price, buy_time, buy_order_id,
                rsi_at_entry, atr_at_entry, change_pct_at_entry,
                macd_crossover_fresh, rvol_at_entry)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, provider, shares, buy_price, buy_time.isoformat(), buy_order_id,
             rsi_at_entry, atr_at_entry, change_pct_at_entry,
             int(macd_crossover_fresh) if macd_crossover_fresh is not None else None,
             rvol_at_entry),
        )
        return cur.lastrowid


def update_trailing_stop_order(position_id: int, order_id: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE positions SET trailing_stop_order_id = ? WHERE id = ?",
            (order_id, position_id),
        )


def update_hard_stop_order(position_id: int, order_id: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE positions SET hard_stop_order_id = ? WHERE id = ?",
            (order_id, position_id),
        )


def close_position(position_id: int, sell_price: float, sell_time: datetime, pnl: float):
    with _connect() as conn:
        conn.execute(
            """UPDATE positions
               SET status = 'closed', sell_price = ?, sell_time = ?, pnl = ?
               WHERE id = ?""",
            (sell_price, sell_time.isoformat(), pnl, position_id),
        )


def mark_stop_tightened(position_id: int, new_stop_order_id: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE positions SET stop_tightened = 1, trailing_stop_order_id = ? WHERE id = ?",
            (new_stop_order_id, position_id),
        )


def get_open_positions(provider: Optional[str] = None) -> List[sqlite3.Row]:
    with _connect() as conn:
        if provider:
            return conn.execute(
                "SELECT * FROM positions WHERE status = 'open' AND provider = ?",
                (provider,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM positions WHERE status = 'open'"
        ).fetchall()


def get_open_position_count(provider: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status = 'open' AND provider = ?",
            (provider,),
        ).fetchone()
        return row[0] if row else 0


def init_wallet(screener_id: str, starting_balance: float) -> None:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with _connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO wallets
               (screener_id, initial_balance, current_balance, day_start_balance, day_date)
               VALUES (?, ?, ?, ?, ?)""",
            (screener_id, starting_balance, starting_balance, starting_balance, today),
        )


def get_wallet(screener_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM wallets WHERE screener_id = ?", (screener_id,)
        ).fetchone()
        return dict(row) if row else None


def reset_day_wallet(screener_id: str, today_date: str, reconciled_balance: float) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE wallets
               SET current_balance = ?, day_start_balance = ?, day_date = ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE screener_id = ?""",
            (reconciled_balance, reconciled_balance, today_date, screener_id),
        )


def update_wallet_cash(screener_id: str, delta: float) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE wallets
               SET current_balance = current_balance + ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE screener_id = ?""",
            (delta, screener_id),
        )


def is_ticker_on_cooldown(symbol: str, cooldown_seconds: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            """SELECT 1 FROM ticker_alerts
               WHERE symbol = ?
                 AND alerted_at > datetime('now', ? || ' seconds')""",
            (symbol, f"-{cooldown_seconds}"),
        ).fetchone()
        return row is not None


def record_ticker_alert(symbol: str):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO ticker_alerts (symbol, alerted_at) VALUES (?, datetime('now'))",
            (symbol,),
        )


def save_price_bar(
    symbol: str,
    timestamp: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: int,
):
    with _connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO price_bars
               (symbol, timestamp, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (symbol, timestamp.isoformat(), open_, high, low, close, volume),
        )
