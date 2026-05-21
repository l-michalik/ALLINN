from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def plot_buy_and_hold(data: pd.DataFrame, output_path: Path, initial_balance: float) -> None:
    dates = mdates.date2num(data["datetime"].to_numpy())
    close_prices = data["close"].to_numpy()
    buy_hold_balance = initial_balance * (close_prices / close_prices[0])

    fig, ax = plt.subplots(figsize=(18, 8))
    ax.plot(dates, buy_hold_balance, color="#0f172a", linewidth=1.4)
    ax.autoscale_view()
    ax.margins(x=0.01, y=0.03)

    ax.set_title("SPXUSD H1 Buy-and-Hold Value", fontsize=14, pad=10)
    ax.set_xlabel("Date")
    ax.set_ylabel("Portfolio value (USD)")
    locator = mdates.AutoDateLocator(minticks=5, maxticks=9)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
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
    fig.tight_layout()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the base buy-and-hold chart from parquet OHLC data.")
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "spxusd_h1.parquet")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "results" / "base.png")
    parser.add_argument("--limit", type=int, default=0, help="Number of latest rows to plot. Use 0 for all rows.")
    parser.add_argument("--initial-balance", type=float, default=10_000.0)
    args = parser.parse_args()

    data = pd.read_parquet(args.input)
    data = data.sort_values("datetime")

    if args.limit > 0:
        data = data.tail(args.limit)

    plot_buy_and_hold(data, args.output, args.initial_balance)
    print(f"Saved buy-and-hold chart to {args.output}")


if __name__ == "__main__":
    main()
