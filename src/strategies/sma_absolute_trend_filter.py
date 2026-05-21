from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from src import backtesting

STRATEGY_NAME = "sma_absolute_trend_filter"
SYMBOL = "SPXUSD"
CURRENCY = "PLN"
BACKTEST_START = pd.Timestamp("2012-01-01")


@dataclass(frozen=True)
class StrategyParams:
    universe: str
    sma_window_days: int
    threshold_value: float
    direction: str

    @property
    def label(self) -> str:
        threshold = f"band {self.threshold_value:.0%}"
        return f"SMA{self.sma_window_days} {threshold} {self.direction}"


DEFAULT_PARAMS = StrategyParams(
    universe=SYMBOL,
    sma_window_days=200,
    threshold_value=0.0,
    direction="long-only",
)


def build_parameter_grid() -> list[StrategyParams]:
    return [
        StrategyParams(
            universe=SYMBOL,
            sma_window_days=sma_window_days,
            threshold_value=threshold_value,
            direction=direction,
        )
        for sma_window_days in [200]
        for threshold_value in [0.0, 0.01, 0.02]
        for direction in ["long-only", "long-short"]
    ]


def warmup_days(parameter_grid: list[StrategyParams]) -> int:
    return max(params.sma_window_days for params in parameter_grid)


def calculate_strategy_result(daily: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    result = daily[["datetime", "open", "high", "low", "close"]].copy()
    close = result["close"]
    sma = close.rolling(params.sma_window_days).mean()
    distance_from_sma = close / sma - 1.0

    signal = pd.Series(0.0, index=result.index, dtype="float64")
    signal[distance_from_sma > params.threshold_value] = 1.0
    if params.direction == "long-short":
        signal[distance_from_sma < -params.threshold_value] = -1.0

    daily_returns = close.pct_change().fillna(0.0)
    position = signal.shift(1).fillna(0.0)

    result["position"] = position
    result["asset_return"] = daily_returns
    result["strategy_return"] = position * daily_returns

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a 200-day SMA absolute trend filter strategy on SPXUSD H1 data.")
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "spxusd_h1.parquet")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "results" / STRATEGY_NAME)
    parser.add_argument("--initial-balance", type=float, default=10_000.0)
    parser.add_argument(
        "--trading-fee-rate",
        type=float,
        default=None,
        help="Override the default XTB US500 spread model with a fixed per-execution notional rate.",
    )
    args = parser.parse_args()

    data = backtesting.load_data(args.input)
    daily = backtesting.build_daily_bars(data)
    parameter_grid = build_parameter_grid()
    start_time = BACKTEST_START
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

    print(f"Saved 200-day SMA trend filter results to {args.output_dir}")


if __name__ == "__main__":
    main()
