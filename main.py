from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import pandas as pd


def plot_candles(data: pd.DataFrame, output_path: Path) -> None:
    dates = mdates.date2num(data["datetime"])
    candle_width = 0.7 / 24

    fig, ax = plt.subplots(figsize=(14, 7))

    for date, open_price, high_price, low_price, close_price in zip(
        dates,
        data["open"],
        data["high"],
        data["low"],
        data["close"],
        strict=True,
    ):
        color = "green" if close_price >= open_price else "red"
        lower_price = min(open_price, close_price)
        candle_height = abs(close_price - open_price)

        ax.vlines(date, low_price, high_price, color=color, linewidth=1)
        ax.add_patch(
            Rectangle(
                (date - candle_width / 2, lower_price),
                candle_width,
                candle_height,
                facecolor=color,
                edgecolor=color,
                linewidth=1,
            )
        )

    ax.set_title("SPXUSD H1 Candlestick Chart")
    ax.set_xlabel("Date")
    ax.set_ylabel("Price")
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d %H:%M"))
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a red/green candlestick chart from parquet OHLC data.")
    parser.add_argument("--input", type=Path, default=Path("spxusd_h1.parquet"))
    parser.add_argument("--output", type=Path, default=Path("candles.png"))
    parser.add_argument("--limit", type=int, default=0, help="Number of latest candles to plot. Use 0 for all rows.")
    args = parser.parse_args()

    data = pd.read_parquet(args.input)
    data = data.sort_values("datetime")

    if args.limit > 0:
        data = data.tail(args.limit)

    plot_candles(data, args.output)
    print(f"Saved candlestick chart to {args.output}")


if __name__ == "__main__":
    main()
