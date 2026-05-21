from __future__ import annotations

import argparse
from dataclasses import dataclass
from itertools import product
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STRATEGY_NAME = "time_series_momentum"
SYMBOL = "SPXUSD"


@dataclass(frozen=True)
class Trade:
    side: int
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    return_pct: float


@dataclass(frozen=True)
class StrategyParams:
    lookback_months: int
    negative_position: str
    volatility_target: float | None

    @property
    def label(self) -> str:
        vol = "no vol" if self.volatility_target is None else f"vol {self.volatility_target:.0%}"
        return f"{self.lookback_months}M {self.negative_position} {vol}"


@dataclass(frozen=True)
class StrategyRun:
    params: StrategyParams
    result: pd.DataFrame
    trades: list[Trade]

    @property
    def roi(self) -> float:
        return float(self.result["strategy_balance"].iloc[-1] / self.result["strategy_balance"].iloc[0] - 1.0)


def load_data(input_path: Path) -> pd.DataFrame:
    data = pd.read_parquet(input_path)
    return data.sort_values("datetime").reset_index(drop=True)


def build_monthly_positions(
    data: pd.DataFrame,
    lookback_months: int,
    negative_position: str,
) -> pd.Series:
    monthly_close = data.set_index("datetime")["close"].resample("ME").last()
    trailing_return = monthly_close / monthly_close.shift(lookback_months) - 1.0
    short_value = -1 if negative_position == "short" else 0

    monthly_signal = pd.Series(0, index=monthly_close.index, dtype="int64")
    monthly_signal[trailing_return > 0] = 1
    monthly_signal[trailing_return < 0] = short_value

    signal_by_month = monthly_signal.copy()
    signal_by_month.index = signal_by_month.index.to_period("M")
    trade_months = data["datetime"].dt.to_period("M")

    return trade_months.map(signal_by_month.shift(1)).fillna(0).astype("int64")


def build_exposure(data: pd.DataFrame, params: StrategyParams) -> pd.Series:
    positions = build_monthly_positions(data, params.lookback_months, params.negative_position).astype("float64")
    if params.volatility_target is None:
        return positions

    daily_close = data.set_index("datetime")["close"].resample("D").last().dropna()
    daily_volatility = daily_close.pct_change().rolling(63).std() * (252**0.5)
    leverage = (params.volatility_target / daily_volatility).clip(upper=3.0)
    leverage_by_day = leverage.reindex(data["datetime"].dt.floor("D")).ffill().fillna(1.0).to_numpy()

    return positions * leverage_by_day


def calculate_balances(
    data: pd.DataFrame,
    exposure: pd.Series,
    initial_balance: float,
    start_time: pd.Timestamp | None = None,
) -> pd.DataFrame:
    result = data[["datetime", "close"]].copy()
    result["position"] = exposure.to_numpy()
    result["asset_return"] = result["close"].pct_change().fillna(0.0)
    result["strategy_return"] = result["asset_return"] * result["position"]

    active_start = result.index[result["position"] != 0][0] if start_time is None else result.index[result["datetime"] >= start_time][0]
    result = result.loc[active_start:].copy()
    result.iloc[0, result.columns.get_loc("asset_return")] = 0.0
    result.iloc[0, result.columns.get_loc("strategy_return")] = 0.0
    result["buy_hold_balance"] = initial_balance * (1.0 + result["asset_return"]).cumprod()
    result["strategy_balance"] = initial_balance * (1.0 + result["strategy_return"]).cumprod()

    return result.reset_index(drop=True)


def run_strategy(
    data: pd.DataFrame,
    params: StrategyParams,
    initial_balance: float,
    start_time: pd.Timestamp,
) -> StrategyRun:
    exposure = build_exposure(data, params)
    result = calculate_balances(data, exposure, initial_balance, start_time)
    trades = extract_trades(result)

    return StrategyRun(params=params, result=result, trades=trades)


def build_parameter_grid() -> list[StrategyParams]:
    return [
        StrategyParams(lookback, negative_position, volatility_target)
        for lookback, negative_position, volatility_target in product(
            range(3, 13),
            ("short", "flat"),
            (None, 0.15, 0.25),
        )
    ]


def common_start_time(data: pd.DataFrame, max_lookback_months: int) -> pd.Timestamp:
    monthly_close = data.set_index("datetime")["close"].resample("ME").last()
    signal_month = monthly_close.index[max_lookback_months + 1].to_period("M")
    month_rows = data[data["datetime"].dt.to_period("M") == signal_month]

    return pd.Timestamp(month_rows["datetime"].iloc[0])


def extract_trades(result: pd.DataFrame) -> list[Trade]:
    trades: list[Trade] = []
    active_side = 0
    entry_index = 0

    for index, exposure in enumerate(result["position"].to_numpy()):
        side = int(exposure > 0) - int(exposure < 0)
        if side == active_side:
            continue

        if active_side != 0 and index > entry_index:
            exit_index = index - 1
            trades.append(create_trade(result, active_side, entry_index, exit_index))

        active_side = side
        entry_index = index

    if active_side != 0 and len(result) > entry_index:
        trades.append(create_trade(result, active_side, entry_index, len(result) - 1))

    return trades


def create_trade(result: pd.DataFrame, side: int, entry_index: int, exit_index: int) -> Trade:
    entry_price = float(result.loc[entry_index, "close"])
    exit_price = float(result.loc[exit_index, "close"])
    raw_return = exit_price / entry_price - 1.0
    trade_return = raw_return if side == 1 else -raw_return

    return Trade(
        side=side,
        entry_time=pd.Timestamp(result.loc[entry_index, "datetime"]),
        exit_time=pd.Timestamp(result.loc[exit_index, "datetime"]),
        entry_price=entry_price,
        exit_price=exit_price,
        return_pct=trade_return,
    )


def plot_top_runs(top_runs: list[StrategyRun], output_path: Path) -> None:
    base_result = top_runs[0].result
    dates = mdates.date2num(base_result["datetime"].to_numpy())

    fig, ax = plt.subplots(figsize=(18, 9))
    ax.plot(dates, base_result["buy_hold_balance"].to_numpy(), color="#0f172a", linewidth=1.8, label=f"{SYMBOL} buy-and-hold")

    colors = ["#0f766e", "#2563eb", "#b45309", "#7c3aed", "#be123c", "#15803d", "#0891b2", "#a16207", "#4338ca", "#c2410c"]
    for index, run in enumerate(top_runs):
        run_dates = mdates.date2num(run.result["datetime"].to_numpy())
        label = f"#{index + 1} {run.params.label} ROI {format_pct(run.roi)}"
        ax.plot(run_dates, run.result["strategy_balance"].to_numpy(), linewidth=1.2, color=colors[index], label=label)

    ax.set_title(f"{SYMBOL} | {STRATEGY_NAME} | top {len(top_runs)} parameter sets", fontsize=14, pad=10)
    ax.set_xlabel("Date")
    ax.set_ylabel("Portfolio balance")
    ax.legend(frameon=False, fontsize=8, ncols=2)
    style_time_axis(ax)

    save_plot(fig, output_path)


def style_time_axis(ax: plt.Axes) -> None:
    locator = mdates.AutoDateLocator(minticks=5, maxticks=9)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.margins(x=0.01, y=0.03)
    ax.grid(True, axis="y", alpha=0.12)
    ax.grid(False, axis="x")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#cbd5e1")
    ax.spines["bottom"].set_color("#cbd5e1")
    ax.tick_params(colors="#334155")
    ax.xaxis.label.set_color("#334155")
    ax.yaxis.label.set_color("#334155")
    ax.title.set_color("#0f172a")


def save_plot(fig: plt.Figure, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def max_drawdown(balance: pd.Series) -> float:
    drawdown = balance / balance.cummax() - 1.0
    return float(drawdown.min())


def format_money(value: float) -> str:
    return f"${value:,.2f}"


def format_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def write_summary(
    output_path: Path,
    top_runs: list[StrategyRun],
    initial_balance: float,
) -> None:
    best_run = top_runs[0]

    lines = [
        f"# {SYMBOL} - {STRATEGY_NAME}",
        "",
        "## Top 10 Parameters",
        "",
        f"- Period: {best_run.result['datetime'].iloc[0]} -> {best_run.result['datetime'].iloc[-1]}",
        f"- Start balance: {format_money(initial_balance)}",
        f"- Buy-and-hold ROI: {format_pct(float(best_run.result['buy_hold_balance'].iloc[-1] / initial_balance - 1.0))}",
        "",
        "| Rank | Params | Final Balance | ROI | Max DD | Trades | Win Rate | Best Trade | Worst Trade |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for index, run in enumerate(top_runs, start=1):
        returns = [trade.return_pct for trade in run.trades]
        winners = [value for value in returns if value > 0]
        best_trade = max(returns) if returns else 0.0
        worst_trade = min(returns) if returns else 0.0
        win_rate = len(winners) / len(returns) if returns else 0.0
        lines.append(
            f"| {index} | {run.params.label} | {format_money(float(run.result['strategy_balance'].iloc[-1]))} | "
            f"{format_pct(run.roi)} | {format_pct(max_drawdown(run.result['strategy_balance']))} | {len(run.trades)} | "
            f"{format_pct(win_rate)} | {format_pct(best_trade)} | {format_pct(worst_trade)} |"
        )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a monthly time-series momentum strategy on SPXUSD H1 data.")
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "spxusd_h1.parquet")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "results" / STRATEGY_NAME)
    parser.add_argument("--initial-balance", type=float, default=10_000.0)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    data = load_data(args.input)
    parameter_grid = build_parameter_grid()
    start_time = common_start_time(data, max(params.lookback_months for params in parameter_grid))
    runs = [run_strategy(data, params, args.initial_balance, start_time) for params in parameter_grid]
    top_runs = sorted(runs, key=lambda run: run.roi, reverse=True)[: args.top]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    plot_top_runs(top_runs, args.output_dir / "chart.png")
    write_summary(args.output_dir / "summary.md", top_runs, args.initial_balance)

    print(f"Saved time-series momentum results to {args.output_dir}")


if __name__ == "__main__":
    main()
