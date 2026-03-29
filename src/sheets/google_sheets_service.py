from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

from src.sheets.schema import SHEET_HEADER


def _now_iso_by_env_tz() -> str:
    tz_name = os.getenv("GOOGLE_SHEETS_TIMEZONE", "Asia/Bangkok").strip() or "Asia/Bangkok"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).isoformat()


@dataclass(frozen=True)
class TradeLogRow:
    timestamp: str
    symbol: str
    strategy: str
    side: str  # "Long"/"Short"
    entry_price: Optional[float]
    exit_price: Optional[float]
    profit_loss_pct: Optional[float]
    status: str  # "Open" | "Closed"


class GoogleSheetsLogger:
    """
    Append trade lifecycle rows:
    - Open: logs Entry Price and Status=Open
    - Close: logs Exit Price, Profit/Loss (%), Status=Closed
    """

    def __init__(
        self,
        *,
        service_account_json_path: str,
        sheet_name: str,
        worksheet_name: Optional[str] = None,
    ) -> None:
        self.sheet_name = sheet_name
        self.worksheet_name = worksheet_name

        # Service account JSON is loaded from a file path (NOT the JSON content).
        with open(service_account_json_path, "r", encoding="utf-8") as f:
            raw_json = json.load(f)

        creds = Credentials.from_service_account_info(
            raw_json,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        self.gc = gspread.authorize(creds)

        self.sh = self.gc.open(self.sheet_name)
        self.ws = (
            self.sh.worksheet(self.worksheet_name)
            if self.worksheet_name
            else self.sh.sheet1
        )

        self._ensure_header()

    def _ensure_header(self) -> None:
        values = self.ws.get_all_values()
        if not values:
            self.ws.append_row(SHEET_HEADER, value_input_option="USER_ENTERED")
            return

        # If first row doesn't match (or has missing columns), attempt to patch header.
        if len(values[0]) < len(SHEET_HEADER) or values[0][: len(SHEET_HEADER)] != SHEET_HEADER:
            try:
                self.ws.update("A1", [SHEET_HEADER], value_input_option="USER_ENTERED")
            except Exception:
                # If header patch fails, keep going; append_row still works.
                pass

    def append_trade_row(self, row: TradeLogRow) -> None:
        try:
            data = [
                row.timestamp,
                row.symbol,
                row.strategy,
                row.side,
                "" if row.entry_price is None else float(row.entry_price),
                "" if row.exit_price is None else float(row.exit_price),
                "" if row.profit_loss_pct is None else float(row.profit_loss_pct),
                row.status,
            ]
            self.ws.append_row(data, value_input_option="USER_ENTERED")
        except Exception as e:
            # Don't crash the trading loop if Sheets is temporarily unavailable.
            print("Google Sheets logging failed:", repr(e))

    def log_open(
        self,
        *,
        symbol: str,
        strategy: str,
        side: str,
        entry_price: float,
    ) -> None:
        self.append_trade_row(
            TradeLogRow(
                timestamp=_now_iso_by_env_tz(),
                symbol=symbol,
                strategy=strategy,
                side=side,
                entry_price=entry_price,
                exit_price=None,
                profit_loss_pct=None,
                status="Open",
            )
        )

    def log_close(
        self,
        *,
        symbol: str,
        strategy: str,
        side: str,
        entry_price: float,
        exit_price: float,
        profit_loss_pct: float,
    ) -> None:
        self.append_trade_row(
            TradeLogRow(
                timestamp=_now_iso_by_env_tz(),
                symbol=symbol,
                strategy=strategy,
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                profit_loss_pct=profit_loss_pct,
                status="Closed",
            )
        )

    def update_dashboard_last_update_time(self, dashboard_name: str = "dashboard") -> None:
        """
        Update <dashboard_name>!B1 with:
        last update time yyyy-MM-dd HH:mm:ss
        """
        try:
            dashboard_ws = self.sh.worksheet(dashboard_name)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            dashboard_ws.update(
                "B1",
                [[f"last update time {now_str}"]],
                value_input_option="USER_ENTERED",
            )
        except Exception as e:
            # Keep the bot running if dashboard sheet is unavailable.
            print("Google Sheets dashboard update failed:", repr(e))

