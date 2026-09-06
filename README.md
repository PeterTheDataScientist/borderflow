# BorderFlow

**Beitbridge is the busiest inland border in Southern Africa and nobody publishes how long it takes to cross. This does not pretend to know either. It measures what is measurable, daily, and says so.**

[![License: Apache 2.0](https://img.shields.io/badge/code-Apache%202.0-blue.svg)](LICENSE)
[![Data: CC BY 4.0](https://img.shields.io/badge/data-CC%20BY%204.0-green.svg)](data/LICENSE)

---

Every haulier, freight forwarder and manufacturer moving goods between South
Africa and Zimbabwe plans drivers, fuel, perishables and penalty clauses around a
queue that has no public measurement. Delays at Beitbridge are reported in the
trade press whenever they become severe, and are invisible the rest of the time.

The obvious project is a wait time forecast. That project cannot honestly be
built yet, and the reason is the most important thing in this repository.

## The forecast is deliberately missing

There is **no public ground truth for crossing times**. No agency publishes them,
no open dataset holds them, and a model trained on nothing can only be scored
against nothing. A forecast with no labels is a guess with a chart on top, and
shipping one would be the fastest possible way to lose any reader who actually
works the corridor, because they know what the queue did last Tuesday and the
chart does not.

So version one forecasts nothing and measures what is labelled.

## What it does measure

**Seaport throughput at the three ports that feed the corridor**: Durban, Beira
and Maputo. IMF PortWatch derives daily port calls and cargo volumes from AIS
vessel tracking and publishes them openly. That series is dated, checkable,
updated daily, and it leads inland border load by days, because a ship that
discharges in Durban on Monday becomes trucks on the N1 later in the week.

That is a real signal about corridor pressure, available today, that nobody has
assembled for this corridor before.

## How the border layer gets built

Not by modelling. By collecting labels.

The observation channel comes first: a driver or a dispatcher records a crossing
in about thirty seconds, and those records accumulate into the ground truth that
does not currently exist anywhere. Only once there are enough labels to hold out a
test set does a forecast get built, and when it does it ships with the error rate
measured on crossings it never saw.

Until then the repository publishes the corridor's upstream state and is explicit
that it is doing so.

## Design decisions worth knowing about

**A network failure is never recorded as an absence of shipping.** Every run
appends a record, successful or not, with the error text when it failed. The
failed runs are the more important half: a gap in the series with a recorded
failure beside it is documented, and a gap with nothing beside it is a mystery
that quietly becomes fiction. `SourceUnreachable` exists as a distinct exception
type specifically so that no caller can conflate the two by accident.

**The endpoint is discovered, not hardcoded.** PortWatch publishes through ArcGIS
Hub, whose pages render client side, so the underlying service URL is not in the
page source and is not stable enough to copy out of a browser. Each run resolves
the dataset id through the Hub API and writes the URL it resolved to into the run
log. A collector that guesses a URL and silently returns nothing when it changes
is worse than one that fails loudly.

**A service error that arrives with HTTP 200 is still an error.** ArcGIS answers
malformed queries with a success status and an error object in the body. Reading
that as zero rows would record a service fault as a day on which no ship called
anywhere on the corridor. It raises instead.

**Unknown fields are kept.** The source publishes a rolling window, so a field not
captured today is not recoverable tomorrow. Every attribute the service returns is
carried into the record verbatim, whether or not anything currently reads it.

**Append-only JSONL in git, not a database.** Git history is the provenance
record, every change is a diff, cloning gets the whole archive with no
credentials, and it costs nothing. Deduplication is by port and day, so re-running
a day is idempotent and the schedule can be aggressive without corrupting the
series.

## Running it

```bash
pip install -e ".[dev]"
pytest                          # 13 tests, no network
python -m borderflow.cli --data data
```

No API key. No account.

## Data

| File | What it is | Licence |
|---|---|---|
| `data/observations.jsonl` | One port on one day, with every field the source returned. Append only. | CC BY 4.0 |
| `data/runs.jsonl` | Every collection attempt, including the ones that failed and why. Append only. | CC BY 4.0 |

Source: [IMF PortWatch](https://portwatch.imf.org), open, no account required.

## Status

Collection is the product right now. This dataset is worth nothing on its first
day and a great deal after ninety, which is the whole reason the schedule is
running before the analysis exists.

## Licence

Code Apache 2.0. Data CC BY 4.0. Chosen so a company can build on this without
asking anyone.
