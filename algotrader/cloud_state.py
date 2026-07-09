from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
DEFAULT_DB_PATH = Path("data") / "optiontrader.db"
DEFAULT_USER_EMAIL = "local-owner@optiontrader.local"
DEFAULT_ACCOUNT_NAME = "Default Paper Account"
SCHEMA_VERSION = 1


def now_ist_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def stable_id(namespace: str, *parts: object) -> str:
    raw = ":".join([namespace, *[str(part) for part in parts]])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def db_path_from_env() -> Path:
    raw = os.getenv("OPTIONTRADER_DB_PATH", "").strip()
    return Path(raw) if raw else DEFAULT_DB_PATH


@dataclass(frozen=True)
class PaperContext:
    user_id: str
    user_email: str
    paper_account_id: str
    paper_account_name: str


class CloudStateStore:
    """Small SQLite-backed store for self-hosted multi-user paper foundations.

    The current engine can keep using JSON while this store mirrors paper state
    into relational tables. That gives us a safe migration path toward full
    per-user paper accounts without deleting or resetting existing state.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else db_path_from_env()

    @classmethod
    def from_env(cls) -> "CloudStateStore":
        return cls(db_path_from_env())

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def session(self):
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.session() as conn:
            self._initialize(conn)

    def _initialize(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_accounts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                starting_capital REAL NOT NULL,
                current_cash REAL NOT NULL,
                realized_pnl REAL NOT NULL DEFAULT 0,
                unrealized_pnl REAL NOT NULL DEFAULT 0,
                max_daily_loss REAL NOT NULL DEFAULT 0,
                max_open_positions INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, name)
            );

            CREATE TABLE IF NOT EXISTS strategy_definitions (
                slug TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                family TEXT NOT NULL,
                status TEXT NOT NULL,
                public_description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS strategy_versions (
                id TEXT PRIMARY KEY,
                strategy_slug TEXT NOT NULL REFERENCES strategy_definitions(slug) ON DELETE CASCADE,
                version TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}',
                enabled_default INTEGER NOT NULL DEFAULT 0,
                rollout_status TEXT NOT NULL DEFAULT 'stable',
                created_at TEXT NOT NULL,
                UNIQUE(strategy_slug, version)
            );

            CREATE TABLE IF NOT EXISTS user_strategy_settings (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                paper_account_id TEXT NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
                strategy_slug TEXT NOT NULL REFERENCES strategy_definitions(slug) ON DELETE CASCADE,
                strategy_version_id TEXT NOT NULL REFERENCES strategy_versions(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 0,
                user_config_json TEXT NOT NULL DEFAULT '{}',
                risk_config_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, paper_account_id, strategy_slug)
            );

            CREATE TABLE IF NOT EXISTS paper_account_state (
                paper_account_id TEXT PRIMARY KEY REFERENCES paper_accounts(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                saved_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                open_trades_count INTEGER NOT NULL DEFAULT 0,
                closed_trades_count INTEGER NOT NULL DEFAULT 0,
                realized_pnl REAL NOT NULL DEFAULT 0,
                capital_committed REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_open_trades (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                paper_account_id TEXT NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                tradingsymbol TEXT NOT NULL,
                instrument_type TEXT NOT NULL,
                strategy_bucket TEXT NOT NULL,
                option_side TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                current_price REAL,
                stop_loss REAL NOT NULL,
                target_price REAL NOT NULL,
                position_value REAL NOT NULL,
                opened_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(paper_account_id, tradingsymbol, opened_at)
            );

            CREATE TABLE IF NOT EXISTS paper_closed_trades (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                paper_account_id TEXT NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                tradingsymbol TEXT NOT NULL,
                instrument_type TEXT NOT NULL,
                strategy_bucket TEXT NOT NULL,
                option_side TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                gross_pnl REAL NOT NULL,
                total_charges REAL NOT NULL,
                pnl REAL NOT NULL,
                exit_reason TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(paper_account_id, tradingsymbol, opened_at, closed_at)
            );

            CREATE TABLE IF NOT EXISTS market_data_cache (
                provider TEXT NOT NULL,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL DEFAULT '',
                data_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                last_price REAL,
                as_of TEXT NOT NULL,
                expires_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(provider, exchange, symbol, interval, data_type)
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                id TEXT PRIMARY KEY,
                user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                actor_type TEXT NOT NULL,
                event_type TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def ensure_default_context(
        self,
        *,
        capital: float,
        max_daily_loss: float = 0.0,
        max_open_positions: int = 0,
    ) -> PaperContext:
        email = os.getenv("OPTIONTRADER_DEFAULT_USER_EMAIL", DEFAULT_USER_EMAIL).strip().lower()
        display_name = os.getenv("OPTIONTRADER_DEFAULT_USER_NAME", "Local Owner").strip()
        account_name = os.getenv("OPTIONTRADER_DEFAULT_ACCOUNT_NAME", DEFAULT_ACCOUNT_NAME).strip()
        user_id = stable_id("optiontrader-user", email)
        account_id = stable_id("optiontrader-paper-account", email, account_name)
        now = now_ist_iso()
        with self.session() as conn:
            self._initialize(conn)
            conn.execute(
                """
                INSERT INTO users(id, email, display_name, role, status, created_at, updated_at)
                VALUES(?, ?, ?, 'admin', 'active', ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    display_name=excluded.display_name,
                    updated_at=excluded.updated_at
                """,
                (user_id, email, display_name, now, now),
            )
            conn.execute(
                """
                INSERT INTO paper_accounts(
                    id, user_id, name, starting_capital, current_cash, realized_pnl,
                    unrealized_pnl, max_daily_loss, max_open_positions, status, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, 0, 0, ?, ?, 'active', ?, ?)
                ON CONFLICT(user_id, name) DO UPDATE SET
                    starting_capital=excluded.starting_capital,
                    max_daily_loss=excluded.max_daily_loss,
                    max_open_positions=excluded.max_open_positions,
                    updated_at=excluded.updated_at
                """,
                (
                    account_id,
                    user_id,
                    account_name,
                    float(capital),
                    float(capital),
                    float(max_daily_loss or 0.0),
                    int(max_open_positions or 0),
                    now,
                    now,
                ),
            )
        return PaperContext(
            user_id=user_id,
            user_email=email,
            paper_account_id=account_id,
            paper_account_name=account_name,
        )

    def seed_default_strategies(
        self,
        *,
        scanner_enabled: bool,
        index_scanner_enabled: bool,
    ) -> None:
        strategies = [
            {
                "slug": "nifty250_2m_engulfing_scanner",
                "display_name": "NIFTY250 2m Engulfing Scanner",
                "family": "stock_options",
                "enabled_default": scanner_enabled,
                "description": "Scans NIFTY250 stocks for 2-minute engulfing/reversal setups and papers ATM options.",
            },
            {
                "slug": "index_options_scanner",
                "display_name": "Index Options Scanner",
                "family": "index_options",
                "enabled_default": index_scanner_enabled,
                "description": "Scans NIFTY, BANKNIFTY, and SENSEX for directional index option paper entries.",
            },
            {
                "slug": "watchlist_directional_stock_options",
                "display_name": "Watchlist Directional - Stock CE/PE",
                "family": "watchlist_options",
                "enabled_default": True,
                "description": "Runs watchlist stock-option directional paper trades from configured symbols.",
            },
            {
                "slug": "watchlist_directional_index_options",
                "display_name": "Watchlist Directional - Index CE/PE",
                "family": "watchlist_options",
                "enabled_default": True,
                "description": "Runs watchlist index-option directional paper trades from configured indexes.",
            },
        ]
        now = now_ist_iso()
        with self.session() as conn:
            self._initialize(conn)
            for strategy in strategies:
                slug = strategy["slug"]
                version_id = stable_id("optiontrader-strategy-version", slug, "local-v1")
                conn.execute(
                    """
                    INSERT INTO strategy_definitions(slug, display_name, family, status, public_description, created_at, updated_at)
                    VALUES(?, ?, ?, 'stable', ?, ?, ?)
                    ON CONFLICT(slug) DO UPDATE SET
                        display_name=excluded.display_name,
                        family=excluded.family,
                        public_description=excluded.public_description,
                        updated_at=excluded.updated_at
                    """,
                    (
                        slug,
                        strategy["display_name"],
                        strategy["family"],
                        strategy["description"],
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO strategy_versions(id, strategy_slug, version, config_json, enabled_default, rollout_status, created_at)
                    VALUES(?, ?, 'local-v1', '{}', ?, 'stable', ?)
                    ON CONFLICT(strategy_slug, version) DO UPDATE SET
                        enabled_default=excluded.enabled_default,
                        rollout_status=excluded.rollout_status
                    """,
                    (version_id, slug, 1 if strategy["enabled_default"] else 0, now),
                )

    def ensure_default_strategy_settings(self, context: PaperContext) -> None:
        now = now_ist_iso()
        with self.session() as conn:
            self._initialize(conn)
            rows = conn.execute(
                """
                SELECT sv.id, sv.strategy_slug, sv.enabled_default
                FROM strategy_versions sv
                WHERE sv.version = 'local-v1'
                """
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO user_strategy_settings(
                        id, user_id, paper_account_id, strategy_slug, strategy_version_id,
                        enabled, user_config_json, risk_config_json, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, '{}', '{}', ?, ?)
                    ON CONFLICT(user_id, paper_account_id, strategy_slug) DO NOTHING
                    """,
                    (
                        stable_id(
                            "optiontrader-user-strategy",
                            context.user_id,
                            context.paper_account_id,
                            row["strategy_slug"],
                        ),
                        context.user_id,
                        context.paper_account_id,
                        row["strategy_slug"],
                        row["id"],
                        int(row["enabled_default"]),
                        now,
                        now,
                    ),
                )

    def list_users(self) -> list[dict[str, Any]]:
        with self.session() as conn:
            self._initialize(conn)
            return [dict(row) for row in conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()]

    def list_paper_accounts(self, user_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM paper_accounts"
        params: tuple[Any, ...] = ()
        if user_id:
            query += " WHERE user_id = ?"
            params = (user_id,)
        query += " ORDER BY created_at"
        with self.session() as conn:
            self._initialize(conn)
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def list_strategy_settings(self, context: PaperContext) -> list[dict[str, Any]]:
        with self.session() as conn:
            self._initialize(conn)
            rows = conn.execute(
                """
                SELECT
                    uss.id,
                    uss.strategy_slug,
                    uss.enabled,
                    uss.user_config_json,
                    uss.risk_config_json,
                    sd.display_name,
                    sd.family,
                    sd.status,
                    sv.version,
                    sv.rollout_status
                FROM user_strategy_settings uss
                JOIN strategy_definitions sd ON sd.slug = uss.strategy_slug
                JOIN strategy_versions sv ON sv.id = uss.strategy_version_id
                WHERE uss.user_id = ? AND uss.paper_account_id = ?
                ORDER BY sd.display_name
                """,
                (context.user_id, context.paper_account_id),
            ).fetchall()
        return [
            {
                **dict(row),
                "enabled": bool(row["enabled"]),
                "user_config": json.loads(row["user_config_json"] or "{}"),
                "risk_config": json.loads(row["risk_config_json"] or "{}"),
            }
            for row in rows
        ]

    def set_strategy_enabled(self, context: PaperContext, strategy_slug: str, enabled: bool) -> None:
        now = now_ist_iso()
        with self.session() as conn:
            self._initialize(conn)
            result = conn.execute(
                """
                UPDATE user_strategy_settings
                SET enabled = ?, updated_at = ?
                WHERE user_id = ? AND paper_account_id = ? AND strategy_slug = ?
                """,
                (1 if enabled else 0, now, context.user_id, context.paper_account_id, strategy_slug),
            )
            if result.rowcount == 0:
                raise KeyError(f"Strategy setting not found: {strategy_slug}")

    def save_paper_state(self, context: PaperContext, payload: dict[str, Any]) -> None:
        account = payload.get("account", {}) if isinstance(payload.get("account"), dict) else {}
        open_trades = list(payload.get("open_trades", []) or [])
        closed_trades = list(payload.get("closed_trades", []) or [])
        saved_at = str(payload.get("saved_at") or now_ist_iso())
        realized_pnl = float(account.get("realized_pnl", 0.0) or 0.0)
        capital_committed = float(account.get("capital_committed", 0.0) or 0.0)
        starting_capital = float(account.get("starting_capital", 0.0) or 0.0)
        available_balance = float(account.get("available_balance", 0.0) or 0.0)
        unrealized_pnl = self._unrealized_pnl(open_trades)
        now = now_ist_iso()
        with self.session() as conn:
            self._initialize(conn)
            conn.execute(
                """
                INSERT INTO paper_account_state(
                    paper_account_id, user_id, saved_at, payload_json, open_trades_count,
                    closed_trades_count, realized_pnl, capital_committed, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(paper_account_id) DO UPDATE SET
                    saved_at=excluded.saved_at,
                    payload_json=excluded.payload_json,
                    open_trades_count=excluded.open_trades_count,
                    closed_trades_count=excluded.closed_trades_count,
                    realized_pnl=excluded.realized_pnl,
                    capital_committed=excluded.capital_committed,
                    updated_at=excluded.updated_at
                """,
                (
                    context.paper_account_id,
                    context.user_id,
                    saved_at,
                    json_dumps(payload),
                    len(open_trades),
                    len(closed_trades),
                    realized_pnl,
                    capital_committed,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE paper_accounts
                SET starting_capital = COALESCE(NULLIF(?, 0), starting_capital),
                    current_cash = ?,
                    realized_pnl = ?,
                    unrealized_pnl = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    starting_capital,
                    available_balance,
                    realized_pnl,
                    unrealized_pnl,
                    now,
                    context.paper_account_id,
                ),
            )
            conn.execute(
                "DELETE FROM paper_open_trades WHERE paper_account_id = ?",
                (context.paper_account_id,),
            )
            for trade in open_trades:
                self._insert_open_trade(conn, context, trade, now)
            for trade in closed_trades:
                self._insert_closed_trade(conn, context, trade, now)

    def load_paper_state(self, context: PaperContext) -> dict[str, Any] | None:
        with self.session() as conn:
            self._initialize(conn)
            row = conn.execute(
                "SELECT payload_json FROM paper_account_state WHERE paper_account_id = ?",
                (context.paper_account_id,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"])

    def migrate_json_state(self, context: PaperContext, state_path: Path | str) -> dict[str, Any]:
        path = Path(state_path)
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not payload:
            payload = {
                "saved_at": now_ist_iso(),
                "account": {},
                "open_trades": [],
                "closed_trades": [],
            }
        self.save_paper_state(context, payload)
        return self.summary(context)

    def summary(self, context: PaperContext) -> dict[str, Any]:
        with self.session() as conn:
            self._initialize(conn)
            account = conn.execute(
                "SELECT * FROM paper_accounts WHERE id = ?",
                (context.paper_account_id,),
            ).fetchone()
            state = conn.execute(
                "SELECT * FROM paper_account_state WHERE paper_account_id = ?",
                (context.paper_account_id,),
            ).fetchone()
            open_count = conn.execute(
                "SELECT COUNT(*) AS count FROM paper_open_trades WHERE paper_account_id = ?",
                (context.paper_account_id,),
            ).fetchone()["count"]
            closed_count = conn.execute(
                "SELECT COUNT(*) AS count FROM paper_closed_trades WHERE paper_account_id = ?",
                (context.paper_account_id,),
            ).fetchone()["count"]
            strategy_count = conn.execute(
                "SELECT COUNT(*) AS count FROM user_strategy_settings WHERE paper_account_id = ?",
                (context.paper_account_id,),
            ).fetchone()["count"]
        return {
            "db_path": str(self.path),
            "user_id": context.user_id,
            "user_email": context.user_email,
            "paper_account_id": context.paper_account_id,
            "paper_account_name": context.paper_account_name,
            "paper_account": dict(account) if account else None,
            "state_saved_at": state["saved_at"] if state else None,
            "open_trades": int(open_count),
            "closed_trades": int(closed_count),
            "strategy_settings": int(strategy_count),
        }

    def upsert_market_data_cache(
        self,
        *,
        provider: str,
        exchange: str,
        symbol: str,
        data_type: str,
        payload: dict[str, Any],
        interval: str = "",
        last_price: float | None = None,
        as_of: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        now = now_ist_iso()
        as_of_value = as_of or now
        with self.session() as conn:
            self._initialize(conn)
            conn.execute(
                """
                INSERT INTO market_data_cache(
                    provider, exchange, symbol, interval, data_type, payload_json,
                    last_price, as_of, expires_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, exchange, symbol, interval, data_type) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    last_price=excluded.last_price,
                    as_of=excluded.as_of,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    provider,
                    exchange,
                    symbol,
                    interval,
                    data_type,
                    json_dumps(payload),
                    last_price,
                    as_of_value,
                    expires_at,
                    now,
                ),
            )

    def get_market_data_cache(
        self,
        *,
        provider: str,
        exchange: str,
        symbol: str,
        data_type: str,
        interval: str = "",
    ) -> dict[str, Any] | None:
        with self.session() as conn:
            self._initialize(conn)
            row = conn.execute(
                """
                SELECT * FROM market_data_cache
                WHERE provider = ? AND exchange = ? AND symbol = ? AND interval = ? AND data_type = ?
                """,
                (provider, exchange, symbol, interval, data_type),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json") or "{}")
        return data

    def _insert_open_trade(
        self,
        conn: sqlite3.Connection,
        context: PaperContext,
        trade: dict[str, Any],
        updated_at: str,
    ) -> None:
        opened_at = str(trade.get("opened_at") or "")
        tradingsymbol = str(trade.get("tradingsymbol") or "")
        conn.execute(
            """
            INSERT INTO paper_open_trades(
                id, user_id, paper_account_id, symbol, exchange, tradingsymbol,
                instrument_type, strategy_bucket, option_side, quantity, entry_price,
                current_price, stop_loss, target_price, position_value, opened_at,
                payload_json, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_account_id, tradingsymbol, opened_at) DO UPDATE SET
                current_price=excluded.current_price,
                stop_loss=excluded.stop_loss,
                target_price=excluded.target_price,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                stable_id("paper-open-trade", context.paper_account_id, tradingsymbol, opened_at),
                context.user_id,
                context.paper_account_id,
                str(trade.get("symbol") or ""),
                str(trade.get("exchange") or ""),
                tradingsymbol,
                str(trade.get("instrument_type") or ""),
                strategy_bucket_for_trade(trade),
                str(trade.get("option_side") or ""),
                int(trade.get("quantity") or 0),
                float(trade.get("entry_price") or 0.0),
                _optional_float(trade.get("current_price")),
                float(trade.get("stop_loss") or 0.0),
                float(trade.get("target_price") or 0.0),
                abs(float(trade.get("entry_price") or 0.0) * int(trade.get("quantity") or 0)),
                opened_at,
                json_dumps(trade),
                updated_at,
            ),
        )

    def _insert_closed_trade(
        self,
        conn: sqlite3.Connection,
        context: PaperContext,
        trade: dict[str, Any],
        updated_at: str,
    ) -> None:
        opened_at = str(trade.get("opened_at") or "")
        closed_at = str(trade.get("closed_at") or "")
        tradingsymbol = str(trade.get("tradingsymbol") or "")
        conn.execute(
            """
            INSERT INTO paper_closed_trades(
                id, user_id, paper_account_id, symbol, exchange, tradingsymbol,
                instrument_type, strategy_bucket, option_side, quantity, entry_price,
                exit_price, gross_pnl, total_charges, pnl, exit_reason, opened_at,
                closed_at, payload_json, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_account_id, tradingsymbol, opened_at, closed_at) DO UPDATE SET
                exit_price=excluded.exit_price,
                gross_pnl=excluded.gross_pnl,
                total_charges=excluded.total_charges,
                pnl=excluded.pnl,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                stable_id("paper-closed-trade", context.paper_account_id, tradingsymbol, opened_at, closed_at),
                context.user_id,
                context.paper_account_id,
                str(trade.get("symbol") or ""),
                str(trade.get("exchange") or ""),
                tradingsymbol,
                str(trade.get("instrument_type") or ""),
                strategy_bucket_for_trade(trade),
                str(trade.get("option_side") or ""),
                int(trade.get("quantity") or 0),
                float(trade.get("entry_price") or 0.0),
                float(trade.get("exit_price") or 0.0),
                float(trade.get("gross_pnl", trade.get("pnl", 0.0)) or 0.0),
                float(trade.get("total_charges") or 0.0),
                float(trade.get("pnl") or 0.0),
                str(trade.get("exit_reason") or ""),
                opened_at,
                closed_at,
                json_dumps(trade),
                updated_at,
            ),
        )

    @staticmethod
    def _unrealized_pnl(open_trades: list[dict[str, Any]]) -> float:
        total = 0.0
        for trade in open_trades:
            current = _optional_float(trade.get("current_price"))
            if current is None:
                continue
            entry = float(trade.get("entry_price") or 0.0)
            quantity = int(trade.get("quantity") or 0)
            if str(trade.get("direction", "BUY")).upper() == "BUY":
                total += (current - entry) * quantity
            else:
                total += (entry - current) * quantity
        return total


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def strategy_bucket_for_trade(trade: dict[str, Any]) -> str:
    entry_reason = str(trade.get("entry_reason", "") or "").lower()
    instrument_type = str(trade.get("instrument_type", "") or "").lower()
    option_side = str(trade.get("option_side", "") or "").upper()
    direction = str(trade.get("direction", "") or "").upper()
    if "nifty250_2m_scanner" in entry_reason:
        return "NIFTY250 2m Engulfing Scanner"
    if "index_options_scanner" in entry_reason:
        return "Index Options Scanner"
    if instrument_type == "stock_option":
        return f"Watchlist Directional - Stock {option_side or direction or 'OPTION'}"
    if instrument_type == "index_option":
        return f"Watchlist Directional - Index {option_side or direction or 'OPTION'}"
    if instrument_type in {"stock_future", "index_future"}:
        return f"Watchlist Directional - {instrument_type.replace('_', ' ').title()}"
    return f"Watchlist Directional - {instrument_type.replace('_', ' ').title() or 'Unknown'}"
