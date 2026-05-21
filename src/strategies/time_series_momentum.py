from __future__ import annotations

import argparse
from dataclasses import dataclass
import random
from pathlib import Path
import sys

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from src import backtesting

STRATEGY_NAME = "time_series_momentum"
SYMBOL = "SPXUSD"
CURRENCY = "PLN"


@dataclass(frozen=True)
class StrategyParams:
    universe: str
    lookback_months: int
    holding_period: str
    threshold_type: str
    threshold_value: float
    direction: str
    vol_target: float | None
    vol_window_days: int
    stop_loss_atr: float | None
    weighting: str

    @property
    def label(self) -> str:
        threshold = f"{self.threshold_type}:{self.threshold_value:g}"
        vol = "no vol" if self.vol_target is None else f"vol {self.vol_target:.0%}"
        stop = "no stop" if self.stop_loss_atr is None else f"sl {self.stop_loss_atr:.1f}ATR"
        return (
            f"{self.lookback_months}M {self.holding_period} {threshold} "
            f"{self.direction} {vol} {self.vol_window_days}D {stop} {self.weighting}"
        )


DEFAULT_PARAMS = StrategyParams(
    universe=SYMBOL,
    lookback_months=6,
    holding_period="1W",
    threshold_type="pct",
    threshold_value=0.01,
    direction="long-short",
    vol_target=0.15,
    vol_window_days=60,
    stop_loss_atr=2.5,
    weighting="inverse_vol",
)


def build_parameter_grid() -> list[StrategyParams]:
    threshold_variants = (
        ("pct", 0.00),
        ("pct", 0.01),
        ("pct", 0.02),
        ("zscore", 0.5),
        ("zscore", 1.0),
    )
    universe = SYMBOL
    lookbacks = [1, 3, 6, 12]
    holding_periods = ["1D", "1W", "1M"]
    directions = ["long-only", "long-short"]
    vol_targets = [None, 0.10, 0.20]
    vol_windows = [20, 60, 126]
    stop_losses = [None, 2.0, 3.0]
    weightings = ["equal", "inverse_vol", "risk_parity"]

    grid = [
        StrategyParams(
            universe=universe,
            lookback_months=lookback_months,
            holding_period=holding_period,
            threshold_type=threshold_type,
            threshold_value=threshold_value,
            direction=direction,
            vol_target=vol_target,
            vol_window_days=vol_window_days,
            stop_loss_atr=stop_loss_atr,
            weighting=weighting,
        )
        for lookback_months in lookbacks
        for holding_period in holding_periods
        for threshold_type, threshold_value in threshold_variants
        for direction in directions
        for vol_target in vol_targets
        for vol_window_days in vol_windows
        for stop_loss_atr in stop_losses
        for weighting in weightings
    ]

    rng = random.Random(42)
    rng.shuffle(grid)
    return grid[:100]


def warmup_days(parameter_grid: list[StrategyParams]) -> int:
    max_lookback_days = max(params.lookback_months for params in parameter_grid) * 21
    max_vol_window_days = max(params.vol_window_days for params in parameter_grid)
    return max(max_lookback_days, max_vol_window_days) + 1


def calculate_strategy_result(daily: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    result = daily[["datetime", "open", "high", "low", "close"]].copy()
    close = result["close"]
    prev_close = close.shift(1)

    lookback_bars = max(1, params.lookback_months * 21)
    momentum = close / close.shift(lookback_bars) - 1.0

    if params.threshold_type == "pct":
        raw_signal = pd.Series(0, index=result.index, dtype="float64")
        raw_signal[momentum > params.threshold_value] = 1.0
        if params.direction == "long-short":
            raw_signal[momentum < -params.threshold_value] = -1.0
        else:
            raw_signal[momentum < -params.threshold_value] = 0.0
    else:
        z_score = (momentum - momentum.rolling(params.vol_window_days).mean()) / momentum.rolling(params.vol_window_days).std()
        raw_signal = pd.Series(0, index=result.index, dtype="float64")
        raw_signal[z_score > params.threshold_value] = 1.0
        if params.direction == "long-short":
            raw_signal[z_score < -params.threshold_value] = -1.0
        else:
            raw_signal[z_score < -params.threshold_value] = 0.0

    holding_period_days = {
        "1D": 1,
        "1W": 5,
        "1M": 21,
    }[params.holding_period]
    rebalance_mask = pd.Series((result.index % holding_period_days) == 0, index=result.index)
    rebalance_signal = raw_signal.where(rebalance_mask)

    daily_returns = close.pct_change().fillna(0.0)
    true_range = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - prev_close).abs(),
            (result["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(params.vol_window_days).mean()

    leverage_by_day = pd.Series(1.0, index=result.index, dtype="float64")
    if params.weighting != "equal":
        inverse_vol = (1.0 / (daily_returns.rolling(params.vol_window_days).std() * (252**0.5))).replace([pd.NA, pd.NaT], 1.0)
        leverage_by_day = inverse_vol.fillna(1.0)

    if params.vol_target is not None:
        vol_scale = (params.vol_target / (daily_returns.rolling(params.vol_window_days).std() * (252**0.5))).clip(upper=3.0)
        leverage_by_day = leverage_by_day * vol_scale.fillna(1.0)

    position_history: list[float] = []
    strategy_returns: list[float] = []
    buy_hold_returns = daily_returns.to_list()

    current_position = 0.0
    entry_price: float | None = None

    for index in result.index:
        if index == 0:
            position_history.append(0.0)
            strategy_returns.append(0.0)
            if not pd.isna(rebalance_signal.iloc[index]):
                current_position = float(rebalance_signal.iloc[index]) * float(leverage_by_day.iloc[index])
                entry_price = float(close.iloc[index]) if current_position != 0 else None
            continue

        position_history.append(current_position)
        strategy_return = current_position * float(daily_returns.iloc[index])

        if current_position != 0 and params.stop_loss_atr is not None and entry_price is not None and not pd.isna(atr.iloc[index]):
            stop_distance = params.stop_loss_atr * float(atr.iloc[index])
            if current_position > 0:
                stop_price = entry_price - stop_distance
                if float(result["low"].iloc[index]) <= stop_price:
                    strategy_return = current_position * (stop_price / float(prev_close.iloc[index]) - 1.0)
                    current_position = 0.0
                    entry_price = None
            else:
                stop_price = entry_price + stop_distance
                if float(result["high"].iloc[index]) >= stop_price:
                    strategy_return = current_position * (stop_price / float(prev_close.iloc[index]) - 1.0)
                    current_position = 0.0
                    entry_price = None

        strategy_returns.append(strategy_return)

        if not pd.isna(rebalance_signal.iloc[index]):
            current_position = float(rebalance_signal.iloc[index]) * float(leverage_by_day.iloc[index])
            entry_price = float(close.iloc[index]) if current_position != 0 else None

    result["position"] = position_history
    result["asset_return"] = buy_hold_returns
    result["strategy_return"] = strategy_returns

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a moving-average time-series momentum strategy on SPXUSD H1 data.")
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "spxusd_h1.parquet")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "results" / STRATEGY_NAME)
    parser.add_argument("--initial-balance", type=float, default=10_000.0)
    parser.add_argument("--trading-fee-rate", type=float, default=backtesting.BINANCE_SPOT_TAKER_FEE_RATE)
    args = parser.parse_args()

    data = backtesting.load_data(args.input)
    daily = backtesting.build_daily_bars(data)
    parameter_grid = build_parameter_grid()
    start_time = backtesting.common_start_time(daily, warmup_days(parameter_grid))
    runs = [
        backtesting.run_backtest(
            calculate_strategy_result(daily, params),
            params,
            args.initial_balance,
            start_time,
            args.trading_fee_rate,
        )
        for params in parameter_grid
    ]
    top_runs = sorted(runs, key=lambda run: run.annualized_roi, reverse=True)[:10]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    backtesting.plot_strategy_balance(
        top_runs,
        args.output_dir / "chart.png",
        SYMBOL,
        CURRENCY,
        len(parameter_grid),
        args.trading_fee_rate,
    )
    backtesting.write_summary(
        args.output_dir / "summary.md",
        top_runs,
        args.initial_balance,
        len(parameter_grid),
        SYMBOL,
        STRATEGY_NAME,
        CURRENCY,
        args.trading_fee_rate,
    )

    print(f"Saved time-series momentum results to {args.output_dir}")


if __name__ == "__main__":
    main()
