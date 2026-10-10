# Sealed Pulse

A daily price tracker for English sealed Pokémon TCG product (booster boxes,
Elite Trainer Boxes, booster bundles, packs and collections), built as an
AI-assisted engineering project with Claude.

**Live dashboard:** the GitHub Pages link in this repository's About panel.
It updates every day after TCGplayer's prices refresh.

## What it does

- Collects TCGplayer market prices for about 3,000 sealed products once a day
  from [TCGCSV](https://tcgcsv.com), the free public mirror of TCGplayer's
  catalog, and stores each day as one write-once Parquet file in Cloudflare R2.
- Separates real "sealed box" products from hundreds of unnumbered cards that
  resemble them.
- Computes trend and risk metrics that are robust to outliers and preorder pricing, and ranks the products most deserving of attention.
- Publishes this dashboard: the most relevant products, their attention category,
  a limited price chart per product, and set-level returns.

```mermaid
flowchart LR
  A[TCGCSV daily feed] --> B[collect: classify + quality gate]
  B --> C[(R2: daily Parquet)]
  C --> D[metrics: DuckDB SQL]
  D --> E[gold layer]
  E --> F[public dashboard]
```

## How it was built with AI

The project was planned and built working with Claude, where I made the product, 
data-source, and visualization decisions. The process:

1. **Research.** A survey of sealed-price data sources, their terms of use,
   and a code review of three similar open-source trackers.
2. **A decisions log.** Every design choice was written down with its owner
   (me or Claude) and the reason the decision was made, before any code was written.
3. **Test-first code.** Every module was written against tests first and the
   plan was then rebuilt and re-run to prove it was complete.
4. **Validation on real data.** Running the pipeline on real prices exposed
   bugs that became regression tests that synthetic tests had missed:
   - a naive "sealed product" rule admitted 551 cards, 16% of its total
   - a single bad price inflated one product's drawdown from −18% to −28%
   - preorder prices made a newly released set look −90% over 90 days
   - `ASOF` turned out to be a reserved word in DuckDB

## What is in this repository

This is a showcase copy of the plumbing and presentation code from one of several data sources:
the TCGCSV client, storage layer, quality gate, snapshot builder, collector, dashboard
renderer, and their tests. The metric definitions, product classifiers,
curated product data, and the underlying price history are kept in a private repo.

## Data and attribution

Prices are TCGplayer market prices, mirrored by TCGCSV. History from before
this project's own daily collection began comes from the open-source
[etfhanstorz/pokemon-tracker](https://github.com/etfhanstorz/pokemon-tracker)
project, used with credit and marked as seed data on the page. It will be
removed at the author's request. Nothing here is financial advice.
