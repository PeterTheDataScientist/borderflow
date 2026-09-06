"""Daily collection of seaport throughput for the Southern African corridor.

**Why this exists before the product does.** The value of this dataset is
entirely a function of how long it has been running. On day one it is a curiosity;
after ninety days it is the only continuous record of corridor load that anybody
outside the shipping lines holds. Starting the cron is therefore the highest
value thing that can be done today, and it costs one scheduled job.

**What is deliberately not here.** No border wait time forecast. Beitbridge is the
busiest inland border in Southern Africa and its queue is unmeasured in public, so
there is no ground truth to score a forecast against. A forecast with no labels is
a guess with a chart, and shipping one would be the fastest way to lose a serious
reader. What can be measured today is upstream: seaport throughput at the three
ports feeding the corridor, published daily by IMF PortWatch from AIS vessel
tracking. That is a real, dated, checkable series, and it leads inland border load
by days. The border layer becomes a forecast when it has labels, and not before.

**The endpoint is discovered, not declared.** PortWatch publishes through ArcGIS
Hub, whose dataset pages are rendered client side, so the underlying FeatureServer
URL is not in the page source and is not stable enough to hardcode from a browser
session. Rather than guess a URL and ship a collector that silently returns
nothing when it changes, the collector resolves the dataset id through the Hub
API each run and records the URL it resolved to in the run log. If the resolution
fails, that is recorded as a resolution failure rather than as an absence of
shipping, which is the distinction that matters most in this whole file.

A network timeout must never be published as a claim about the world. Recording
"we could not reach the source" and "the source says nothing happened" as the same
value is how a dataset quietly becomes fiction.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

# ArcGIS Hub dataset id for the PortWatch daily port activity layer. The id is
# stable across endpoint changes, which is exactly why resolution goes through it.
PORTWATCH_DATASET = "959214444157458aad969389b3ebe1a0_0"
HUB_API = "https://hub.arcgis.com/api/v3/datasets/{dataset_id}"

# The corridor. Durban and Maputo feed the road route north through Beitbridge;
# Beira feeds the Beira corridor into eastern Zimbabwe and Zambia. Named here
# rather than passed in, because the corridor is the subject of the project and a
# configurable port list would invite the dataset to lose its identity.
CORRIDOR_PORTS: tuple[str, ...] = ("Durban", "Beira", "Maputo")

USER_AGENT = (
    "BorderFlow/0.1 (+https://github.com/PeterTheDataScientist/borderflow; "
    "open dataset of Southern African corridor activity)"
)


class SourceUnreachable(RuntimeError):
    """The source could not be reached or did not answer in a usable shape.

    A distinct exception type rather than an empty result, so that no caller can
    accidentally record a network failure as a day with no shipping.
    """


@dataclass(frozen=True)
class RunRecord:
    """One collection attempt, successful or not. Appended every run, always.

    The failed runs are the more important half. A gap in the observation series
    with no run record beside it is unexplained; a gap with a recorded failure is
    documented. Publishing only the successes is how a dataset acquires an
    invisible survivorship bias.
    """

    checked_at: str
    dataset_id: str
    resolved_url: str | None
    ok: bool
    rows: int
    ports_seen: tuple[str, ...] = ()
    error: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


@dataclass(frozen=True)
class Observation:
    """One port on one day, as the source reported it."""

    port: str
    date: str
    checked_at: str
    source: str
    values: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Identity of the measurement, so a re-run cannot duplicate a row."""
        return f"{self.port}|{self.date}"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def resolve_endpoint(client: httpx.Client, dataset_id: str = PORTWATCH_DATASET) -> str:
    """Ask ArcGIS Hub where the layer actually lives.

    Raises rather than returning a default. A collector that falls back to a
    guessed URL on failure produces data whose provenance nobody can reconstruct.
    """
    url = HUB_API.format(dataset_id=dataset_id)
    try:
        r = client.get(url, headers={"Accept": "application/json"})
        r.raise_for_status()
        payload = r.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise SourceUnreachable(f"could not resolve {dataset_id}: {exc}") from exc

    attrs = (payload.get("data") or {}).get("attributes") or {}
    for key in ("url", "serviceUrl", "layerUrl"):
        if attrs.get(key):
            return str(attrs[key])
    raise SourceUnreachable(
        f"hub returned no service url for {dataset_id}; keys were {sorted(attrs)}"
    )


def fetch_rows(
    client: httpx.Client, endpoint: str, ports: Sequence[str] = CORRIDOR_PORTS
) -> list[dict[str, Any]]:
    """Query the layer for the corridor ports.

    The where clause is built from the port list rather than pulling the whole
    global layer and filtering locally: the layer covers every major port on
    earth, and moving all of it daily to keep three rows would be rude to a free
    public service as well as slow.
    """
    quoted = ", ".join(f"'{p}'" for p in ports)
    params = {
        "where": f"portname IN ({quoted})",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
        "resultRecordCount": "2000",
    }
    try:
        r = client.get(f"{endpoint.rstrip('/')}/query", params=params)
        r.raise_for_status()
        payload = r.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise SourceUnreachable(f"query failed against {endpoint}: {exc}") from exc

    if "error" in payload:
        raise SourceUnreachable(f"service returned an error: {payload['error']}")
    return [f.get("attributes", {}) for f in payload.get("features", [])]


def to_observations(
    rows: Iterable[dict[str, Any]], checked_at: dt.datetime, source: str
) -> list[Observation]:
    """Normalise service rows into observations, keeping every field.

    Field names in the source are not assumed beyond the two the record needs to
    be identifiable. Everything else is carried through verbatim, because a
    collector that drops fields it does not currently understand throws away the
    only copy of them: the source publishes a rolling window, so a field not
    captured today is not recoverable tomorrow.
    """
    out: list[Observation] = []
    for row in rows:
        port = _first(row, ("portname", "PORTNAME", "port_name", "port"))
        date = _first(row, ("date", "DATE", "obs_date", "day"))
        if not port or date is None:
            continue
        out.append(
            Observation(
                port=str(port),
                date=_as_date(date),
                checked_at=checked_at.isoformat(),
                source=source,
                values={k: v for k, v in row.items() if v is not None},
            )
        )
    return out


def _first(row: dict[str, Any], keys: Sequence[str]) -> Any:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _as_date(value: Any) -> str:
    """Normalise a date to ISO, accepting the epoch milliseconds ArcGIS emits."""
    if isinstance(value, int | float):
        return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC).date().isoformat()
    text = str(value)
    return text[:10]


def append_new(path: Path, observations: Sequence[Observation]) -> int:
    """Append only observations whose key is not already on file.

    Append-only JSONL in the repository rather than a database. Git history is the
    provenance record, every change is a diff, cloning gets the whole archive with
    no credentials, and it costs nothing. Deduplication by key rather than by line
    means a re-run of the same day is idempotent, which is what allows the schedule
    to be aggressive without corrupting the series.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen.add(f"{rec.get('port')}|{rec.get('date')}")

    fresh = [o for o in observations if o.key not in seen]
    if fresh:
        with path.open("a", encoding="utf-8") as fh:
            for o in fresh:
                fh.write(o.to_json() + "\n")
    return len(fresh)


def append_run(path: Path, record: RunRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record.to_json() + "\n")


def collect(
    data_dir: Path,
    *,
    ports: Sequence[str] = CORRIDOR_PORTS,
    client: httpx.Client | None = None,
) -> RunRecord:
    """One collection run. Always writes a run record, whatever happens."""
    now = dt.datetime.now(tz=dt.UTC)
    owned = client is None
    client = client or httpx.Client(
        timeout=45.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    )
    endpoint: str | None = None
    try:
        endpoint = resolve_endpoint(client)
        rows = fetch_rows(client, endpoint, ports)
        observations = to_observations(rows, now, endpoint)
        written = append_new(data_dir / "observations.jsonl", observations)
        record = RunRecord(
            checked_at=now.isoformat(),
            dataset_id=PORTWATCH_DATASET,
            resolved_url=endpoint,
            ok=True,
            rows=written,
            ports_seen=tuple(sorted({o.port for o in observations})),
        )
    except SourceUnreachable as exc:
        record = RunRecord(
            checked_at=now.isoformat(),
            dataset_id=PORTWATCH_DATASET,
            resolved_url=endpoint,
            ok=False,
            rows=0,
            error=str(exc)[:400],
        )
    finally:
        if owned:
            client.close()

    append_run(data_dir / "runs.jsonl", record)
    return record
