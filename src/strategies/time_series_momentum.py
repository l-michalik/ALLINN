from __future__ import annotations

import argparse
from dataclasses import dataclass
import random
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STRATEGY_NAME = "time_series_momentum"
SYMBOL = "SPXUSD"
CURRENCY = "PLN"


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


@dataclass(frozen=True)
class StrategyRun:
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


def common_start_time(daily: pd.DataFrame, parameter_grid: list[StrategyParams]) -> pd.Timestamp:
    max_lookback_days = max(params.lookback_months for params in parameter_grid) * 21
    max_vol_window_days = max(params.vol_window_days for params in parameter_grid)
    warmup_days = max(max_lookback_days, max_vol_window_days) + 1
    warmup_days = min(warmup_days, len(daily) - 1)
    return pd.Timestamp(daily["datetime"].iloc[warmup_days])


def calculate_balances(
    daily: pd.DataFrame,
    params: StrategyParams,
    initial_balance: float,
    start_time: pd.Timestamp,
) -> pd.DataFrame:
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

    result = result[result["datetime"] >= start_time].copy()
    result.iloc[0, result.columns.get_loc("asset_return")] = 0.0
    result.iloc[0, result.columns.get_loc("strategy_return")] = 0.0
    result["buy_hold_balance"] = initial_balance * (1.0 + result["asset_return"]).cumprod()
    result["strategy_balance"] = initial_balance * (1.0 + result["strategy_return"]).cumprod()

    return result.reset_index(drop=True)


def run_strategy(
    daily: pd.DataFrame,
    params: StrategyParams,
    initial_balance: float,
    start_time: pd.Timestamp,
) -> StrategyRun:
    result = calculate_balances(daily, params, initial_balance, start_time)
    trades = extract_trades(result)

    return StrategyRun(params=params, result=result, trades=trades)


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


def build_price_candles(data: pd.DataFrame, start_time: pd.Timestamp, frequency: str) -> pd.DataFrame:
    candles = (
        data[data["datetime"] >= start_time]
        .set_index("datetime")
        .resample(frequency)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
        .reset_index()
    )

    return candles


def plot_candles(ax: plt.Axes, candles: pd.DataFrame) -> None:
    dates = mdates.date2num(candles["datetime"].to_numpy())
    if len(dates) > 1:
        width = min((dates[1:] - dates[:-1]).min() * 0.55, 5.0)
    else:
        width = 3.0

    for date, open_price, high, low, close in zip(
        dates,
        candles["open"].to_numpy(),
        candles["high"].to_numpy(),
        candles["low"].to_numpy(),
        candles["close"].to_numpy(),
        strict=True,
    ):
        body_low = min(open_price, close)
        body_height = max(abs(close - open_price), 0.01)
        ax.vlines(date, low, high, color="black", linewidth=0.45, alpha=0.55)
        ax.add_patch(
            plt.Rectangle(
                (date - width / 2, body_low),
                width,
                body_height,
                facecolor="black",
                edgecolor="black",
                linewidth=0.35,
                alpha=0.25,
            )
        )


def plot_strategy_balance(top_runs: list[StrategyRun], output_path: Path) -> None:
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

    ax.set_title(f"{SYMBOL} equity curve | top 10 of 100 combinations", fontsize=14, pad=10)
    ax.set_xlabel("Date")
    ax.set_ylabel(f"Portfolio balance ({CURRENCY})")
    ax.legend(frameon=False, fontsize=8, ncols=2, loc="upper left")
    ax.text(
        0.98,
        0.98,
        f"Buy & hold ROI/yr: {format_pct(buy_hold_annual_roi)}\nBest strategy ROI/yr: {format_pct(best_run.annualized_roi)}",
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=10,
        color="#0f172a",
        bbox=dict(boxstyle="round,pad=0.35", facecolor="white", edgecolor="#cbd5e1", alpha=0.9),
    )
    style_time_axis(ax)
    style_balance_axis(ax)

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


def format_money(value: float) -> str:
    return f"{value:,.2f} {CURRENCY}"


def format_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


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


def write_summary(
    output_path: Path,
    top_runs: list[StrategyRun],
    initial_balance: float,
    combinations_count: int,
) -> None:
    best_run = top_runs[0]
    buy_hold_annual_roi = annualized_return(
        initial_balance,
        float(best_run.result["buy_hold_balance"].iloc[-1]),
        pd.Timestamp(best_run.result["datetime"].iloc[0]),
        pd.Timestamp(best_run.result["datetime"].iloc[-1]),
    )
    lines = [
        f"# {SYMBOL} - {STRATEGY_NAME}",
        "",
        "## Top 10 Results",
        "",
        f"- Period: {best_run.result['datetime'].iloc[0]} -> {best_run.result['datetime'].iloc[-1]}",
        f"- Start balance: {format_money(initial_balance)}",
        f"- Tested combinations: {combinations_count}",
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
            f"| {index} | {run.params.label} | {format_money(float(run.result['strategy_balance'].iloc[-1]))} | "
            f"{format_pct(run.annualized_roi)} | {format_pct(max_drawdown(run.result['strategy_balance']))} | {len(run.trades)} | "
            f"{format_pct(win_rate)} | {format_pct(best_trade)} | {format_pct(worst_trade)} |"
        )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a moving-average time-series momentum strategy on SPXUSD H1 data.")
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "spxusd_h1.parquet")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "results" / STRATEGY_NAME)
    parser.add_argument("--initial-balance", type=float, default=10_000.0)
    args = parser.parse_args()

    data = load_data(args.input)
    daily = build_daily_bars(data)
    parameter_grid = build_parameter_grid()
    start_time = common_start_time(daily, parameter_grid)
    runs = [run_strategy(daily, params, args.initial_balance, start_time) for params in parameter_grid]
    top_runs = sorted(runs, key=lambda run: run.annualized_roi, reverse=True)[:10]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    plot_strategy_balance(top_runs, args.output_dir / "chart.png")
    write_summary(args.output_dir / "summary.md", top_runs, args.initial_balance, len(parameter_grid))

    print(f"Saved time-series momentum results to {args.output_dir}")


if __name__ == "__main__":
    main()
