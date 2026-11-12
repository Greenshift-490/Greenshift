"""Tests for the OASIS client. No real network calls: we fake the HTTP session.

The sample CSV columns mirror the OASIS layout (interval times in GMT). Double-check
them against a real download and update the sample if CAISO's format differs.
"""

import io
import zipfile
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

from greenshift.data.oasis import (
    OasisClient,
    OasisError,
    format_oasis_time,
    pacific_day_range,
    parse_zip,
    save_raw,
    split_range,
)

UTC = timezone.utc

SAMPLE_CSV = """INTERVALSTARTTIME_GMT,INTERVALENDTIME_GMT,OPR_DT,OPR_HR,TAC_AREA_NAME,XML_DATA_ITEM,MW
2026-10-01T07:00:00-00:00,2026-10-01T08:00:00-00:00,2026-10-01,1,CA ISO-TAC,SYS_FCST_DA_MW,21000
2026-10-01T08:00:00-00:00,2026-10-01T09:00:00-00:00,2026-10-01,2,CA ISO-TAC,SYS_FCST_DA_MW,20500
"""

ERROR_XML = """<?xml version="1.0"?>
<m:OASISReport xmlns:m="http://www.caiso.com/soa/OASISReport_v1.xsd">
  <m:ERROR><m:ERR_CODE>1000</m:ERR_CODE><m:ERR_DESC>No data returned</m:ERR_DESC></m:ERROR>
</m:OASISReport>"""


def make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class FakeSession:
    """Returns queued responses in order and records each request's params."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_client(responses):
    session = FakeSession(responses)
    sleeps = []
    client = OasisClient(session=session, sleep=sleeps.append, clock=lambda: 0.0)
    return client, session, sleeps


# --- Time handling ----------------------------------------------------------------

def test_format_converts_pacific_to_utc():
    start, _ = pacific_day_range(date(2026, 10, 1))  # PDT is UTC-7
    assert format_oasis_time(start) == "20261001T07:00-0000"


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError):
        format_oasis_time(datetime(2026, 10, 1))


def test_fall_back_day_is_25_hours():
    start, end = pacific_day_range(date(2026, 11, 1))  # DST ends this day
    assert end - start == timedelta(hours=25)


def test_split_range_makes_contiguous_chunks():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    chunks = split_range(start, start + timedelta(days=70), max_days=31)
    assert len(chunks) == 3
    assert chunks[0][0] == start and chunks[-1][1] == start + timedelta(days=70)
    assert all(a[1] == b[0] for a, b in zip(chunks, chunks[1:]))


# --- Parsing ----------------------------------------------------------------------

def test_parse_zip_reads_csv_with_utc_times():
    df = parse_zip(make_zip({"report.csv": SAMPLE_CSV}))
    assert len(df) == 2
    assert df["interval_start_utc"].iloc[0] == pd.Timestamp("2026-10-01T07:00Z")


def test_parse_zip_raises_on_error_xml():
    with pytest.raises(OasisError) as excinfo:
        parse_zip(make_zip({"INVALID_REQUEST.xml": ERROR_XML}))
    assert excinfo.value.code == "1000"


def test_parse_zip_raises_on_non_zip():
    with pytest.raises(OasisError):
        parse_zip(b"<html>maintenance page</html>")


# --- Client: retries and throttling -----------------------------------------------

DAY = pacific_day_range(date(2026, 10, 1))
OK = FakeResponse(200, make_zip({"report.csv": SAMPLE_CSV}))


def test_fetch_adds_provenance_columns():
    client, session, _ = make_client([OK])
    df = client.fetch("SLD_FCST", "DAM", *DAY)
    assert session.calls[0]["startdatetime"] == "20261001T07:00-0000"
    assert (df["oasis_query"] == "SLD_FCST").all()
    assert df["retrieved_at_utc"].notna().all()


def test_retries_server_error_then_succeeds():
    client, session, sleeps = make_client([FakeResponse(503), OK])
    df = client.fetch("SLD_FCST", "DAM", *DAY)
    assert len(df) == 2 and len(session.calls) == 2
    assert 5 in sleeps  # waited (backoff) before retrying


def test_retries_network_error():
    client, session, _ = make_client([requests.ConnectionError("down"), OK])
    assert len(client.fetch("SLD_FCST", "DAM", *DAY)) == 2


def test_gives_up_after_max_retries():
    client, session, _ = make_client([FakeResponse(503)] * 4)
    with pytest.raises(OasisError, match="Gave up"):
        client.fetch("SLD_FCST", "DAM", *DAY)
    assert len(session.calls) == 4


def test_client_error_is_not_retried():
    client, session, _ = make_client([FakeResponse(400)])
    with pytest.raises(OasisError, match="HTTP 400"):
        client.fetch("SLD_FCST", "DAM", *DAY)
    assert len(session.calls) == 1


def test_long_range_is_split_and_throttled():
    start = datetime(2026, 1, 1, 8, tzinfo=UTC)
    client, session, sleeps = make_client([OK, OK])
    client.fetch("SLD_FCST", "DAM", start, start + timedelta(days=40))
    assert len(session.calls) == 2
    assert 5 in sleeps  # second request waited for the minimum interval


# --- Saving -----------------------------------------------------------------------

def test_save_raw_never_overwrites(tmp_path):
    client, _, _ = make_client([OK, OK])
    first = save_raw(client.fetch("SLD_FCST", "DAM", *DAY), tmp_path)
    second_df = client.fetch("SLD_FCST", "DAM", *DAY)
    second_df["retrieved_at_utc"] += pd.Timedelta(minutes=1)
    second = save_raw(second_df, tmp_path)
    assert first != second and first.exists() and second.exists()
