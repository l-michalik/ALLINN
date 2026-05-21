from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
from pathlib import Path
import sys

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from src import backtesting

STRATEGY_NAME = "donchian_breakout"
SYMBOL = "SPXUSD"
CURRENCY = "PLN"
BACKTEST_START = pd.Timestamp("2012-01-01")


@dataclass(frozen=True)
class StrategyParams:
    universe: str
    lookback_hours: int
    direction: str
    sizing: str
    atr_window_hours: int
    risk_per_trade: float
    max_position_size: float
    stop_loss_atr: float | None
    take_profit_rr: float | None

    @property
    def label(self) -> str:
        stop = "no stop" if self.stop_loss_atr is None else f"sl {self.stop_loss_atr:.1f}ATR"
        target = "no tp" if self.take_profit_rr is None else f"tp {self.take_profit_rr:.1f}R"
        if self.sizing == "full":
            sizing = "full"
        else:
            sizing = f"risk {self.risk_per_trade:.1%} cap {self.max_position_size:.0%}"
        return f"{self.lookback_hours}H {self.direction} {sizing} ATR{self.atr_window_hours} {stop} {target}"


DEFAULT_PARAMS = StrategyParams(
    universe=SYMBOL,
    lookback_hours=43,
    direction="long-short",
    sizing="full",
    atr_window_hours=24,
    risk_per_trade=0.01,
    max_position_size=0.05,
    stop_loss_atr=None,
    take_profit_rr=None,
)


def build_parameter_grid(include_enhanced: bool = False) -> list[StrategyParams]:
    basic_params = [
        StrategyParams(
            universe=SYMBOL,
            lookback_hours=lookback_hours,
            direction=direction,
            sizing="full",
            atr_window_hours=24,
            risk_per_trade=0.01,
            max_position_size=1.0,
            stop_loss_atr=None,
            take_profit_rr=None,
        )
        for lookback_hours in range(1, 169)
        for direction in ["long-only", "long-short"]
    ]
    if not include_enhanced:
        return basic_params

    enhanced_params = [
        StrategyParams(
            universe=SYMBOL,
            lookback_hours=lookback_hours,
            direction=direction,
            sizing="atr",
            atr_window_hours=atr_window_hours,
            risk_per_trade=0.01,
            max_position_size=0.05,
            stop_loss_atr=stop_loss_atr,
            take_profit_rr=2.0,
        )
        for lookback_hours in [24, 43, 72, 120, 168]
        for direction in ["long-only", "long-short"]
        for atr_window_hours in [24, 72]
        for stop_loss_atr in [2.0, 3.0]
    ]
    return basic_params + enhanced_params


def warmup_hours(parameter_grid: list[StrategyParams]) -> int:
    max_lookback = max(params.lookback_hours for params in parameter_grid)
    max_atr_window = max(params.atr_window_hours for params in parameter_grid)
    return max(max_lookback, max_atr_window) + 1


def calculate_strategy_result(data: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    result = data[["datetime", "open", "high", "low", "close"]].copy()
    close = result["close"]
    hourly_returns = close.pct_change().fillna(0.0)

    signal = calculate_donchian_signal(close, params)
    if params.sizing == "full" and params.stop_loss_atr is None and params.take_profit_rr is None:
        position = signal.shift(1).fillna(0.0)
        result["position"] = position
        result["asset_return"] = hourly_returns
        result["strategy_return"] = position * hourly_returns
        return result

    prev_close = close.shift(1)
    atr = calculate_atr(result, prev_close, params.atr_window_hours)

    return calculate_enhanced_strategy_result(result, signal, hourly_returns, atr, params)


def calculate_donchian_signal(close: pd.Series, params: StrategyParams) -> pd.Series:
    upper_channel = close.rolling(params.lookback_hours).max().shift(1)
    lower_channel = close.rolling(params.lookback_hours).min().shift(1)

    signal = pd.Series(index=close.index, dtype="float64")
    signal[close > upper_channel] = 1.0
    signal[close < lower_channel] = -1.0 if params.direction == "long-short" else 0.0
    return signal.ffill().fillna(0.0)


def calculate_atr(result: pd.DataFrame, prev_close: pd.Series, atr_window_hours: int) -> pd.Series:
    true_range = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - prev_close).abs(),
            (result["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(atr_window_hours).mean()


def calculate_enhanced_strategy_result(
    result: pd.DataFrame,
    signal: pd.Series,
    hourly_returns: pd.Series,
    atr: pd.Series,
    params: StrategyParams,
) -> pd.DataFrame:
    close = result["close"]
    close_values = close.to_numpy()
    high_values = result["high"].to_numpy()
    low_values = result["low"].to_numpy()
    signal_values = signal.to_numpy()
    return_values = hourly_returns.to_numpy()
    atr_values = atr.to_numpy()
    position_history: list[float] = []
    strategy_returns: list[float] = []
    current_position = 0.0
    entry_price: float | None = None
    entry_risk: float | None = None

    for index in range(len(result)):
        if index == 0:
            position_history.append(0.0)
            strategy_returns.append(0.0)
            continue

        position_history.append(current_position)
        strategy_return = current_position * float(return_values[index])

        if current_position != 0 and entry_price is not None and entry_risk is not None:
            previous_close = float(close_values[index - 1])
            if current_position > 0:
                stop_price = entry_price - entry_risk
                target_price = entry_price + entry_risk * params.take_profit_rr if params.take_profit_rr is not None else None
                if params.stop_loss_atr is not None and float(low_values[index]) <= stop_price:
                    strategy_return = current_position * (stop_price / previous_close - 1.0)
                    current_position = 0.0
                    entry_price = None
                    entry_risk = None
                elif target_price is not None and float(high_values[index]) >= target_price:
                    strategy_return = current_position * (target_price / previous_close - 1.0)
                    current_position = 0.0
                    entry_price = None
                    entry_risk = None
            else:
                stop_price = entry_price + entry_risk
                target_price = entry_price - entry_risk * params.take_profit_rr if params.take_profit_rr is not None else None
                if params.stop_loss_atr is not None and float(high_values[index]) >= stop_price:
                    strategy_return = current_position * (stop_price / previous_close - 1.0)
                    current_position = 0.0
                    entry_price = None
                    entry_risk = None
                elif target_price is not None and float(low_values[index]) <= target_price:
                    strategy_return = current_position * (target_price / previous_close - 1.0)
                    current_position = 0.0
                    entry_price = None
                    entry_risk = None

        strategy_returns.append(strategy_return)

        next_signal = float(signal_values[index])
        if next_signal == 0.0:
            current_position = 0.0
            entry_price = None
            entry_risk = None
            continue

        next_position = next_signal * position_size(float(close_values[index]), float(atr_values[index]), params)
        if next_position != current_position:
            current_position = next_position
            entry_price = float(close_values[index])
            entry_risk = risk_distance(float(atr_values[index]), params)

    result["position"] = position_history
    result["asset_return"] = hourly_returns
    result["strategy_return"] = strategy_returns

    return result


def position_size(close_value: float, atr_value: float, params: StrategyParams) -> float:
    if params.sizing == "full":
        return 1.0

    stop_distance = atr_value * float(params.stop_loss_atr or 1.0)
    if math.isnan(stop_distance) or stop_distance <= 0.0:
        return 0.0

    risk_sized_position = params.risk_per_trade / (stop_distance / close_value)
    return min(risk_sized_position, params.max_position_size)


def risk_distance(atr_value: float, params: StrategyParams) -> float | None:
    if params.stop_loss_atr is None:
        return None

    if math.isnan(atr_value):
        return None
    return atr_value * params.stop_loss_atr


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Donchian channel breakout strategy on SPXUSD H1 data.")
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "spxusd_h1.parquet")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "results" / STRATEGY_NAME)
    parser.add_argument("--initial-balance", type=float, default=10_000.0)
    parser.add_argument("--trading-fee-rate", type=float, default=backtesting.BINANCE_SPOT_TAKER_FEE_RATE)
    parser.add_argument("--include-enhanced", action="store_true", help="Also run ATR-sized stop-loss/take-profit variants.")
    args = parser.parse_args()

    data = backtesting.load_data(args.input)
    parameter_grid = build_parameter_grid(args.include_enhanced)
    start_time = BACKTEST_START
    runs = [
        backtesting.BacktestRun(
            params=params,
            result=backtesting.calculate_balances(
                calculate_strategy_result(data, params),
                args.initial_balance,
                start_time,
                args.trading_fee_rate,
            ),
            trades=[],
        )
        for params in parameter_grid
    ]
    top_runs = sorted(runs, key=lambda run: run.annualized_roi, reverse=True)[:10]
    top_runs = [
        backtesting.BacktestRun(
            params=run.params,
            result=run.result,
            trades=backtesting.extract_trades(run.result),
        )
        for run in top_runs
    ]

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

    print(f"Saved Donchian breakout results to {args.output_dir}")


if __name__ == "__main__":
    main()
