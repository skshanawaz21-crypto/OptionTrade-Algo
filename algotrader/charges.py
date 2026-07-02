from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChargesBreakdown:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi: float
    gst: float
    stamp_duty: float

    @property
    def total(self) -> float:
        return (
            self.brokerage
            + self.stt
            + self.exchange_txn
            + self.sebi
            + self.gst
            + self.stamp_duty
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "brokerage": self.brokerage,
            "stt": self.stt,
            "exchange_txn": self.exchange_txn,
            "sebi": self.sebi,
            "gst": self.gst,
            "stamp_duty": self.stamp_duty,
        }


def equity_intraday_charges(
    *,
    buy_price: float,
    sell_price: float,
    quantity: int,
    exchange: str = "NSE",
) -> ChargesBreakdown:
    buy_turnover = buy_price * quantity
    sell_turnover = sell_price * quantity
    turnover = buy_turnover + sell_turnover

    brokerage_buy = min(buy_turnover * 0.0003, 20.0)
    brokerage_sell = min(sell_turnover * 0.0003, 20.0)
    brokerage = brokerage_buy + brokerage_sell

    stt = sell_turnover * 0.00025
    exchange_rate = 0.0000297 if exchange.upper() == "NSE" else 0.0000375
    exchange_txn = turnover * exchange_rate
    sebi = turnover * 0.000001
    gst = 0.18 * (brokerage + exchange_txn + sebi)
    stamp_duty = buy_turnover * 0.00003

    return ChargesBreakdown(
        brokerage=brokerage,
        stt=stt,
        exchange_txn=exchange_txn,
        sebi=sebi,
        gst=gst,
        stamp_duty=stamp_duty,
    )


def option_buy_charges(
    *,
    buy_price: float,
    sell_price: float,
    quantity: int,
    exchange: str = "NFO",
) -> ChargesBreakdown:
    buy_turnover = buy_price * quantity
    sell_turnover = sell_price * quantity
    premium_turnover = buy_turnover + sell_turnover

    brokerage = 40.0
    stt = sell_turnover * 0.0015
    exchange_upper = exchange.upper()
    exchange_rate = 0.0003553 if exchange_upper in {"NFO", "NSE"} else 0.000325
    exchange_txn = premium_turnover * exchange_rate
    sebi = premium_turnover * 0.000001
    gst = 0.18 * (brokerage + exchange_txn + sebi)
    stamp_duty = buy_turnover * 0.00003

    return ChargesBreakdown(
        brokerage=brokerage,
        stt=stt,
        exchange_txn=exchange_txn,
        sebi=sebi,
        gst=gst,
        stamp_duty=stamp_duty,
    )
