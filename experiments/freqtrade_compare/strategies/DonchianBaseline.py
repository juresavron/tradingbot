"""Isolated signal-parity experiment; never a live-money deployment strategy.

The vectorized target matches tradingbot.strategy.DonchianBreakout on the
same complete, ordered input history. A truncated/restarted dataframe can lose
the older entry that established the target; startup_candle_count does not
solve that unbounded position-state dependency. compare_signals.py exposes it.
"""

import math
from datetime import datetime
from decimal import Decimal, localcontext

from pandas import DataFrame, Series

from freqtrade.strategy import IStrategy


class DonchianBaseline(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "4h"
    can_short = False
    entry_period = 120
    exit_period = 60
    startup_candle_count = entry_period
    process_only_new_candles = True
    minimal_roi = {}
    trailing_stop = False
    use_custom_stoploss = False
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    position_adjustment_enable = False

    # Freqtrade requires a stoploss. This 99% research sentinel differs from
    # the local strategy's absent price stop and its separate 10% ACCOUNT
    # drawdown kill. It is not a recommended risk policy or kill-switch port.
    stoploss = -0.99
    max_entry_quote = Decimal("75")
    cash_fraction = Decimal("0.25")
    # Local automation uses a 0.002 reserve, distinct from its 0.001 fill fee.
    fee_buffer = Decimal("0.002")

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["prior_entry_high"] = dataframe["high"].shift(1).rolling(self.entry_period).max()
        dataframe["prior_exit_low"] = dataframe["low"].shift(1).rolling(self.exit_period).min()
        ready = dataframe["prior_entry_high"].notna()
        changes = Series(float("nan"), index=dataframe.index, dtype="float64")
        changes.loc[ready & (dataframe["close"] > dataframe["prior_entry_high"])] = 1
        changes.loc[ready & (dataframe["close"] < dataframe["prior_exit_low"])] = 0
        dataframe["target_position"] = changes.ffill().fillna(0).astype("int8")
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Persistent targets, not only breakout edges: starting flat while
        # the seeded strategy already wants long must still permit an entry.
        # No volume filter is added: the local baseline has none.
        dataframe["enter_long"] = dataframe["target_position"].astype("int8")
        dataframe["enter_tag"] = None
        dataframe.loc[dataframe["enter_long"] == 1, "enter_tag"] = "donchian_target_long"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = (1 - dataframe["target_position"]).astype("int8")
        dataframe["exit_tag"] = None
        dataframe.loc[dataframe["exit_long"] == 1, "exit_tag"] = "donchian_target_flat"
        return dataframe

    def custom_stake_amount(
        self, pair: str, current_time: datetime, current_rate: float,
        proposed_stake: float, min_stake: float | None, max_stake: float,
        leverage: float, entry_tag: str | None, side: str, **kwargs,
    ) -> float:
        """Mirror the local pre-quantization entry budget; fail closed.

        Freqtrade still applies its own precision/minimum-order constraints
        and fee accounting. Neither fill nor account-equity parity follows
        from this callback. Use raw free cash, not the globally discounted
        tradable-balance allowance, to match local automation's cash fraction.
        """
        wallets = getattr(self, "wallets", None)
        if wallets is None or side != "long" or leverage != 1:
            return 0.0
        free = wallets.get_free(self.config.get("stake_currency", "EUR"))
        values = [free, current_rate, max_stake]
        if min_stake is not None:
            values.append(min_stake)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            return 0.0
        if free <= 0 or current_rate <= 0 or max_stake <= 0 or min_stake is not None and min_stake < 0:
            return 0.0
        with localcontext() as context:
            context.prec = 100
            budget = min(self.max_entry_quote, Decimal(str(free)) * self.cash_fraction)
            stake = min(budget / (1 + self.fee_buffer), Decimal(str(max_stake)))
            if min_stake is not None and stake < Decimal(str(min_stake)):
                return 0.0  # Do not raise the requested budget to the venue minimum.
            return float(stake)
