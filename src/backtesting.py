from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd


XTB_US500_SPREAD_POINTS = 0.6
XTB_US500_HALF_SPREAD_POINTS = XTB_US500_SPREAD_POINTS / 2.0


class StrategyParams(Protocol):
    @property
    def label(self) -> str: ...


@dataclass(frozen=True)
class Trade:
    side: int
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    return_pct: float


@dataclass(frozen=True)
class BacktestRun:
    params: StrategyParams
    result: pd.DataFrame
    trades: list[Trade]

    @property
    def roi(self) -> float:
        return float(self.result["strategy_balance"].iloc[-1] / self.result["strategy_balance"].iloc[0] - 1.0)

    @property
    def annualized_roi(self) -> float:
        return annualized_return(
            float(self.result["strategy_balance"].iloc[0]),
            float(self.result["strategy_balance"].iloc[-1]),
            pd.Timestamp(self.result["datetime"].iloc[0]),
            pd.Timestamp(self.result["datetime"].iloc[-1]),
        )


def load_data(input_path: Path) -> pd.DataFrame:
    data = pd.read_parquet(input_path)
    return data.sort_values("datetime").reset_index(drop=True)


def build_daily_bars(data: pd.DataFrame) -> pd.DataFrame:
    return (
        data.set_index("datetime")
        .resample("D")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
        .reset_index()
    )


def common_start_time(daily: pd.DataFrame, warmup_days: int) -> pd.Timestamp:
    warmup_days = min(warmup_days, len(daily) - 1)
    return pd.Timestamp(daily["datetime"].iloc[warmup_days])


def run_backtest(
    strategy_result: pd.DataFrame,
    params: StrategyParams,
    initial_balance: float,
    start_time: pd.Timestamp,
    trading_fee_rate: float | None = None,
) -> BacktestRun:
    result = calculate_balances(strategy_result, initial_balance, start_time, trading_fee_rate)
    trades = extract_trades(result)

    return BacktestRun(params=params, result=result, trades=trades)


def calculate_balances(
    strategy_result: pd.DataFrame,
    initial_balance: float,
    start_time: pd.Timestamp,
    trading_fee_rate: float | None = None,
) -> pd.DataFrame:
    result = strategy_result[strategy_result["datetime"] >= start_time].copy()
    position_change = result["position"].diff().abs().fillna(0.0)
    result["trading_fee"] = calculate_trading_cost(position_change, result["close"], trading_fee_rate)
    result["strategy_return"] = result["strategy_return"] - result["trading_fee"]
    result.iloc[0, result.columns.get_loc("asset_return")] = 0.0
    result.iloc[0, result.columns.get_loc("strategy_return")] = 0.0
    result.iloc[0, result.columns.get_loc("trading_fee")] = 0.0
    result["buy_hold_balance"] = initial_balance * (1.0 + result["asset_return"]).cumprod()
    result["strategy_balance"] = initial_balance * (1.0 + result["strategy_return"]).cumprod()

    return result.reset_index(drop=True)


def calculate_trading_cost(position_change: pd.Series, close: pd.Series, trading_fee_rate: float | None) -> pd.Series:
    if trading_fee_rate is not None:
        return position_change * trading_fee_rate

    return position_change * (XTB_US500_HALF_SPREAD_POINTS / close)


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


def plot_strategy_balance(
    top_runs: list[BacktestRun],
    output_path: Path,
    symbol: str,
    currency: str,
    combinations_count: int,
    trading_fee_rate: float | None = None,
) -> None:
    fig, ax = plt.subplots(figsize=(18, 9))
    best_run = top_runs[0]
    run_dates = mdates.date2num(best_run.result["datetime"].to_numpy())
    start_time = pd.Timestamp(best_run.result["datetime"].iloc[0])
    end_time = pd.Timestamp(best_run.result["datetime"].iloc[-1])
    buy_hold_annual_roi = annualized_return(
        float(best_run.result["buy_hold_balance"].iloc[0]),
        float(best_run.result["buy_hold_balance"].iloc[-1]),
        start_time,
        end_time,
    )
    trade_returns = [trade.return_pct for trade in best_run.trades]
    winning_trades = [value for value in trade_returns if value > 0]
    win_rate = len(winning_trades) / len(trade_returns) if trade_returns else 0.0

    for index, run in enumerate(top_runs):
        run_dates = mdates.date2num(run.result["datetime"].to_numpy())
        color = "#16a34a" if index == 0 else "#9ca3af"
        linewidth = 2.4 if index == 0 else 0.9
        alpha = 1.0 if index == 0 else 0.18
        label = f"#{index + 1} ROI/yr {format_pct(run.annualized_roi)}"
        ax.plot(
            run_dates,
            run.result["strategy_balance"].to_numpy(),
            linewidth=linewidth,
            color=color,
            alpha=alpha,
            label=label,
        )

    ax.plot(
        run_dates,
        best_run.result["buy_hold_balance"].to_numpy(),
        linewidth=1.8,
        color="#111827",
        linestyle="--",
        label="Buy and hold",
    )

    ax.set_title(f"{symbol} equity curve | top {len(top_runs)} of {combinations_count} combinations", fontsize=14, pad=10)
    ax.set_xlabel("Date")
    ax.set_ylabel(f"Portfolio balance ({currency})")
    ax.text(
        0.02,
        0.98,
        (
            f"Buy & hold ROI/yr: {format_pct(buy_hold_annual_roi)}\n"
            f"Best strategy ROI/yr: {format_pct(best_run.annualized_roi)}\n"
            f"Trades: {len(best_run.trades)}\n"
            f"Win rate: {format_pct(win_rate)}\n"
            f"Cost: {format_trading_cost(trading_fee_rate)}"
        ),
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=10,
        color="#0f172a",
        bbox=dict(boxstyle="round,pad=0.35", facecolor="white", edgecolor="#cbd5e1", alpha=0.9),
    )
    style_time_axis(ax)
    style_balance_axis(ax)

    save_plot(fig, output_path)


def write_summary(
    output_path: Path,
    top_runs: list[BacktestRun],
    initial_balance: float,
    combinations_count: int,
    symbol: str,
    strategy_name: str,
    currency: str,
    trading_fee_rate: float | None = None,
) -> None:
    best_run = top_runs[0]
    buy_hold_annual_roi = annualized_return(
        initial_balance,
        float(best_run.result["buy_hold_balance"].iloc[-1]),
        pd.Timestamp(best_run.result["datetime"].iloc[0]),
        pd.Timestamp(best_run.result["datetime"].iloc[-1]),
    )
    lines = [
        f"# {symbol} - {strategy_name}",
        "",
        f"## Top {len(top_runs)} Results",
        "",
        f"- Period: {best_run.result['datetime'].iloc[0]} -> {best_run.result['datetime'].iloc[-1]}",
        f"- Start balance: {format_money(initial_balance, currency)}",
        f"- Tested combinations: {combinations_count}",
        f"- Trading cost: {format_trading_cost(trading_fee_rate)}",
        f"- Buy and hold annual ROI: {format_pct(buy_hold_annual_roi)}",
        f"- Best strategy annual ROI: {format_pct(best_run.annualized_roi)}",
        "",
        "| Rank | Params | Final Balance | ROI/yr | Max DD | Trades | Win Rate | Best Trade | Worst Trade |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for index, run in enumerate(top_runs, start=1):
        returns = [trade.return_pct for trade in run.trades]
        winners = [value for value in returns if value > 0]
        best_trade = max(returns) if returns else 0.0
        worst_trade = min(returns) if returns else 0.0
        win_rate = len(winners) / len(returns) if returns else 0.0
        lines.append(
            f"| {index} | {run.params.label} | {format_money(float(run.result['strategy_balance'].iloc[-1]), currency)} | "
            f"{format_pct(run.annualized_roi)} | {format_pct(max_drawdown(run.result['strategy_balance']))} | {len(run.trades)} | "
            f"{format_pct(win_rate)} | {format_pct(best_trade)} | {format_pct(worst_trade)} |"
        )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def style_balance_axis(ax: plt.Axes) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_color("#cbd5e1")
    ax.tick_params(colors="#334155")
    ax.yaxis.label.set_color("#334155")


def save_plot(fig: plt.Figure, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def max_drawdown(balance: pd.Series) -> float:
    drawdown = balance / balance.cummax() - 1.0
    return float(drawdown.min())


def format_money(value: float, currency: str) -> str:
    return f"{value:,.2f} {currency}"


def format_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def format_trading_cost(trading_fee_rate: float | None) -> str:
    if trading_fee_rate is not None:
        return f"{format_pct(trading_fee_rate)} per executed notional"

    return f"XTB US500 spread model ({XTB_US500_SPREAD_POINTS:g} points; half-spread per execution)"


def annualized_return(
    start_value: float,
    end_value: float,
    start_time: pd.Timestamp,
    end_time: pd.Timestamp,
) -> float:
    if start_value <= 0.0 or end_value <= 0.0:
        return -1.0

    elapsed_days = (end_time - start_time).total_seconds() / 86_400
    if elapsed_days <= 0:
        return 0.0

    years = elapsed_days / 365.25
    return (end_value / start_value) ** (1.0 / years) - 1.0
