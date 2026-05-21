# ALLIN

## Base chart

Generate the full SPXUSD H1 buy-and-hold chart from `spxusd_h1.parquet`:

```bash
uv run python scripts/generate_base.py
```

By default, the script shows how a `10,000 USD` investment in `SPXUSD` would grow if held through the full sample. The chart is saved as `results/base.png`.

## Time-series momentum strategy

Run the moving-average time-series momentum strategy on `spxusd_h1.parquet`:

```bash
uv run python src/strategies/time_series_momentum.py
```

By default, the strategy starts with `10,000 PLN` from the same start date for both buy-and-hold and strategy equity curves, tests `100` parameter combinations, shows the top `10` on the chart, and saves the chart plus summary to `results/time_series_momentum/`.

## 200-day SMA absolute trend filter

Run the 200-day SMA absolute trend filter strategy on `spxusd_h1.parquet`:

```bash
uv run python src/strategies/sma_absolute_trend_filter.py
```

The strategy resamples the H1 data to daily bars, goes long when the close is above the 200-day SMA, and stays in cash or goes short when below it depending on the tested direction variant. Results are saved to `results/sma_absolute_trend_filter/`.

## Donchian channel breakout

Run the Donchian channel breakout strategy on `spxusd_h1.parquet`:

```bash
uv run python src/strategies/donchian_breakout.py
```

The strategy follows the report's H1 breakout rule: it buys when the close breaks above the prior rolling channel high and sells or exits when the close breaks below the prior rolling channel low. The default grid tests 1-168 hour lookbacks. Results are saved to `results/donchian_breakout/`.

To also test the slower ATR-sized stop-loss and 2R take-profit variants:

```bash
uv run python src/strategies/donchian_breakout.py --include-enhanced
```
