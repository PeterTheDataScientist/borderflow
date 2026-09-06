"""Tests for the collector, all offline.

The cases that matter are the failure ones. A collector that works when the
service works is easy; a collector that cannot be tricked into recording a
network failure as an absence of shipping is the point.
"""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from borderflow.collect import (
    KNOWN_ENDPOINT,
    PORTWATCH_DATASET,
    Observation,
    SourceUnreachable,
    append_new,
    collect,
    fetch_rows,
    resolve_endpoint,
    to_observations,
)

NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)
ENDPOINT = "https://services.example.invalid/arcgis/rest/services/pw/FeatureServer/0"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------ resolution


def test_endpoint_is_resolved_from_the_hub_api_and_the_route_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        assert PORTWATCH_DATASET in str(request.url)
        return httpx.Response(200, json={"data": {"attributes": {"url": ENDPOINT}}})

    assert resolve_endpoint(_client(handler)) == (ENDPOINT, "hub")


def test_a_hub_response_without_a_url_falls_back_and_says_so():
    """The fallback exists so Hub being down does not cost a day of collection.
    It is only defensible because the route is recorded."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"attributes": {"name": "PortWatch"}}})

    assert resolve_endpoint(_client(handler)) == (KNOWN_ENDPOINT, "known")


def test_a_hub_404_falls_back_rather_than_collecting_nothing():
    """Exactly what the first live run hit, now covered."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    assert resolve_endpoint(_client(handler)) == (KNOWN_ENDPOINT, "known")


def test_a_transport_error_falls_back_to_the_documented_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    assert resolve_endpoint(_client(handler)) == (KNOWN_ENDPOINT, "known")


# ----------------------------------------------------------------- query


def test_the_where_clause_asks_only_for_the_corridor_ports():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["where"] = request.url.params.get("where", "")
        return httpx.Response(200, json={"features": []})

    fetch_rows(_client(handler), ENDPOINT)
    assert "Durban" in seen["where"]
    assert "Beira" in seen["where"]
    assert "Maputo" in seen["where"]


def test_a_service_level_error_payload_is_not_read_as_an_empty_day():
    """ArcGIS answers 200 with an error object. Treating that as zero rows would
    record a service fault as a day on which no ship called anywhere."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": 400, "message": "bad"}})

    with pytest.raises(SourceUnreachable, match="service returned an error"):
        fetch_rows(_client(handler), ENDPOINT)


# ----------------------------------------------------------- normalising


def test_epoch_milliseconds_become_an_iso_date():
    ms = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp() * 1000)
    obs = to_observations([{"portname": "Durban", "date": ms}], NOW, ENDPOINT)
    assert obs[0].date == "2026-09-01"


def test_unknown_fields_are_carried_through_rather_than_dropped():
    """The source publishes a rolling window, so a field not captured today is
    gone tomorrow."""
    row = {"portname": "Beira", "date": "2026-09-01", "some_new_metric": 42}
    obs = to_observations([row], NOW, ENDPOINT)
    assert obs[0].values["some_new_metric"] == 42


def test_rows_without_a_port_or_a_date_are_skipped_not_guessed():
    rows = [{"portname": "Durban"}, {"date": "2026-09-01"}, {}]
    assert to_observations(rows, NOW, ENDPOINT) == []


# ------------------------------------------------------------- appending


def test_appending_is_idempotent_on_the_same_port_and_day(tmp_path):
    path = tmp_path / "observations.jsonl"
    obs = [Observation("Durban", "2026-09-01", NOW.isoformat(), ENDPOINT, {"a": 1})]
    assert append_new(path, obs) == 1
    assert append_new(path, obs) == 0
    assert len(path.read_text().strip().splitlines()) == 1


def test_a_new_day_for_a_known_port_is_appended(tmp_path):
    path = tmp_path / "observations.jsonl"
    append_new(path, [Observation("Durban", "2026-09-01", NOW.isoformat(), ENDPOINT)])
    written = append_new(
        path, [Observation("Durban", "2026-09-02", NOW.isoformat(), ENDPOINT)]
    )
    assert written == 1


# ------------------------------------------------------------- whole run


def test_a_failed_run_is_recorded_and_writes_no_observations(tmp_path):
    """The property the dataset's honesty rests on.

    Resolution now falls back, so the failure has to come from the query itself.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "hub.arcgis.com" in str(request.url):
            return httpx.Response(404, json={"error": "not found"})
        raise httpx.ConnectTimeout("down")

    record = collect(tmp_path, client=_client(handler))

    assert record.ok is False
    assert record.rows == 0
    assert record.error
    assert not (tmp_path / "observations.jsonl").exists()

    runs = (tmp_path / "runs.jsonl").read_text().strip().splitlines()
    assert len(runs) == 1
    assert json.loads(runs[0])["ok"] is False


def test_a_successful_run_records_the_url_it_actually_resolved(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if "hub.arcgis.com" in str(request.url):
            return httpx.Response(200, json={"data": {"attributes": {"url": ENDPOINT}}})
        return httpx.Response(
            200,
            json={
                "features": [
                    {"attributes": {"portname": "Durban", "date": "2026-09-01"}},
                    {"attributes": {"portname": "Beira", "date": "2026-09-01"}},
                ]
            },
        )

    record = collect(tmp_path, client=_client(handler))

    assert record.ok is True
    assert record.rows == 2
    assert record.resolved_url == ENDPOINT
    assert record.route == "hub"
    assert record.ports_seen == ("Beira", "Durban")


def test_every_run_appends_a_record_whether_it_succeeded_or_not(tmp_path):
    def ok(request: httpx.Request) -> httpx.Response:
        if "hub.arcgis.com" in str(request.url):
            return httpx.Response(200, json={"data": {"attributes": {"url": ENDPOINT}}})
        return httpx.Response(200, json={"features": []})

    def bad(request: httpx.Request) -> httpx.Response:
        if "hub.arcgis.com" in str(request.url):
            return httpx.Response(404, json={"error": "not found"})
        raise httpx.ConnectTimeout("down")

    collect(tmp_path, client=_client(ok))
    collect(tmp_path, client=_client(bad))
    collect(tmp_path, client=_client(ok))

    runs = (tmp_path / "runs.jsonl").read_text().strip().splitlines()
    assert [json.loads(r)["ok"] for r in runs] == [True, False, True]
