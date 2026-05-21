# ALLIN

## Base chart

Generate the full SPXUSD H1 buy-and-hold chart from `spxusd_h1.parquet`:

```bash
uv run python scripts/generate_base.py
```

By default, the script shows how a `10,000 USD` investment in `SPXUSD` would grow if held through the full sample. The chart is saved as `results/base.png`.
