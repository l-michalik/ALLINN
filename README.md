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

By default, the strategy starts with `10,000 PLN` from the same start date for both buy-and-hold and strategy equity curves, runs one built-in parameter combination, and saves the chart plus summary to `results/time_series_momentum/`.
