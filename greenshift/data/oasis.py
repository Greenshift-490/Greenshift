"""Client for the CAISO OASIS SingleZip API.

Example (one Pacific-time day of day-ahead demand forecasts):

    from datetime import date
    from greenshift.data.oasis import OasisClient, pacific_day_range

    start, end = pacific_day_range(date(2026, 10, 1))
    df = OasisClient().fetch("SLD_FCST", "DAM", start, end)

Timestamps
    OASIS wants UTC times in the URL. Every function here takes timezone-aware datetimes
    and refuses naive ones, because a naive "00:00" is ambiguous (Pacific? UTC?).
    Returned rows use OASIS's *_GMT columns, parsed into ``interval_start_utc`` and
    ``interval_end_utc``. We deliberately ignore OPR_HR (local hour 1-25), which is
    awkward on daylight-saving change days.

Provenance
    Every row records which query produced it and ``retrieved_at_utc``. Forecasts get
    revised, so this lets us replay exactly what the scheduler knew at decision time.
"""

from __future__ import annotations

import argparse
import io
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

OASIS_URL = "https://oasis.caiso.com/oasisapi/SingleZip"
PACIFIC = ZoneInfo("America/Los_Angeles")

MAX_DAYS_PER_REQUEST = 31        # OASIS rejects longer ranges, so we split them
MIN_SECONDS_BETWEEN_REQUESTS = 5  # be polite: OASIS throttles rapid-fire requests
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OasisError(Exception):
    """OASIS returned an error (HTTP failure, or an error XML inside the zip)."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(f"[{code}] {message}" if code else message)
        self.code = code


# --- Time helpers -----------------------------------------------------------------

def format_oasis_time(dt: datetime) -> str:
    """Format an aware datetime the way OASIS expects, e.g. 20261001T07:00-0000."""
    if dt.tzinfo is None:
        raise ValueError("Naive datetime: pass a timezone-aware datetime instead.")
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H:%M-0000")


def pacific_day_range(day: date, days: int = 1) -> tuple[datetime, datetime]:
    """Return (start, end) in UTC covering whole California calendar days.

    CAISO operating days follow Pacific time, so a day is 23, 24 or 25 hours long
    depending on daylight saving. zoneinfo handles that for us.
    """
    start = datetime.combine(day, datetime.min.time(), tzinfo=PACIFIC)
    end = datetime.combine(day + timedelta(days=days), datetime.min.time(), tzinfo=PACIFIC)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def split_range(start: datetime, end: datetime,
                max_days: int = MAX_DAYS_PER_REQUEST) -> list[tuple[datetime, datetime]]:
    """Split [start, end) into back-to-back chunks no longer than max_days."""
    if end <= start:
        raise ValueError("end must be after start")
    chunks, cursor = [], start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=max_days), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    return chunks


# --- Parsing ----------------------------------------------------------------------

def parse_zip(content: bytes) -> pd.DataFrame:
    """Turn an OASIS zip response into a DataFrame with UTC interval columns.

    OASIS reports many errors with HTTP 200 and an XML file inside the zip instead of
    the CSV, so we check for that explicitly.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise OasisError("Response was not a zip file") from exc

    names = archive.namelist()
    csv_names = [n for n in names if n.lower().endswith(".csv")]
    if not csv_names:
        xml_names = [n for n in names if n.lower().endswith(".xml")]
        if xml_names:
            raise _error_from_xml(archive.read(xml_names[0]))
        raise OasisError(f"Zip contained no CSV (files: {names})")

    df = pd.concat((pd.read_csv(archive.open(n)) for n in csv_names), ignore_index=True)

    for column in ("INTERVALSTARTTIME_GMT", "INTERVALENDTIME_GMT"):
        if column not in df.columns:
            raise OasisError(f"Unexpected CSV format: missing {column}")
    df["interval_start_utc"] = pd.to_datetime(df["INTERVALSTARTTIME_GMT"], utc=True)
    df["interval_end_utc"] = pd.to_datetime(df["INTERVALENDTIME_GMT"], utc=True)
    return df


def _error_from_xml(xml_bytes: bytes) -> OasisError:
    """Pull ERR_CODE / ERR_DESC out of an OASIS error XML (namespace-agnostic)."""
    code = desc = None
    try:
        for element in ET.fromstring(xml_bytes).iter():
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "ERR_CODE":
                code = (element.text or "").strip()
            elif tag == "ERR_DESC":
                desc = (element.text or "").strip()
    except ET.ParseError:
        pass
    return OasisError(desc or "OASIS returned an error XML", code=code)


# --- Client -----------------------------------------------------------------------

class OasisClient:
    """Downloads OASIS reports with throttling, retries and range splitting.

    ``session`` and ``sleep`` can be swapped out in tests so no real network calls or
    real waiting happen.
    """

    def __init__(self, session: requests.Session | None = None, timeout: float = 60,
                 max_retries: int = 3, backoff_seconds: float = 5,
                 min_interval: float = MIN_SECONDS_BETWEEN_REQUESTS,
                 sleep=time.sleep, clock=time.monotonic):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last_request = None

    def fetch(self, query: str, market_run_id: str, start: datetime, end: datetime,
              version: int = 1) -> pd.DataFrame:
        """Download one report for [start, end) and return a single DataFrame."""
        retrieved_at = datetime.now(timezone.utc)
        frames = []
        for chunk_start, chunk_end in split_range(start, end):
            params = {
                "queryname": query,
                "market_run_id": market_run_id,
                "startdatetime": format_oasis_time(chunk_start),
                "enddatetime": format_oasis_time(chunk_end),
                "version": version,
                "resultformat": 6,  # 6 = CSV inside the zip
            }
            frames.append(parse_zip(self._get(params)))

        df = pd.concat(frames, ignore_index=True)
        df["oasis_query"] = query
        df["oasis_market_run_id"] = market_run_id
        df["retrieved_at_utc"] = retrieved_at
        return df.drop_duplicates().sort_values("interval_start_utc", ignore_index=True)

    def _get(self, params: dict) -> bytes:
        """GET with throttling; retry network errors, 429 and 5xx with backoff."""
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                response = self.session.get(OASIS_URL, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                problem = f"network error: {exc}"
                wait = self.backoff_seconds * 2 ** attempt
            else:
                if response.status_code == 200:
                    return response.content
                if response.status_code not in RETRYABLE_STATUS:
                    raise OasisError(f"HTTP {response.status_code} for {params['queryname']}")
                problem = f"HTTP {response.status_code}"
                retry_after = response.headers.get("Retry-After", "")
                wait = (float(retry_after) if retry_after.isdigit()
                        else self.backoff_seconds * 2 ** attempt)
            if attempt < self.max_retries:
                self._sleep(wait)
        raise OasisError(f"Gave up after {self.max_retries + 1} attempts ({problem})")

    def _throttle(self) -> None:
        if self._last_request is not None:
            elapsed = self._clock() - self._last_request
            if elapsed < self.min_interval:
                self._sleep(self.min_interval - elapsed)
        self._last_request = self._clock()


# --- Saving -----------------------------------------------------------------------

def save_raw(df: pd.DataFrame, directory: Path | str = "data/raw") -> Path:
    """Write a download to its own CSV. Never overwrites earlier downloads, so old
    forecasts are preserved for fair replays."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    first = df.iloc[0]
    name = "{q}_{m}_{s:%Y%m%dT%H%MZ}_{e:%Y%m%dT%H%MZ}_retrieved_{r:%Y%m%dT%H%M%SZ}.csv".format(
        q=first["oasis_query"], m=first["oasis_market_run_id"],
        s=df["interval_start_utc"].min(), e=df["interval_end_utc"].max(),
        r=first["retrieved_at_utc"])
    path = directory / name
    if path.exists():
        raise FileExistsError(f"{path} already exists; refusing to overwrite")
    df.to_csv(path, index=False)
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Download a CAISO OASIS report.")
    parser.add_argument("--query", required=True, help="e.g. SLD_FCST or SLD_REN_FCST")
    parser.add_argument("--market", default="DAM", help="market_run_id, e.g. DAM")
    parser.add_argument("--day", required=True, type=date.fromisoformat,
                        help="first Pacific-time day, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--out", default="data/raw")
    args = parser.parse_args(argv)

    start, end = pacific_day_range(args.day, args.days)
    df = OasisClient().fetch(args.query, args.market, start, end)
    path = save_raw(df, args.out)
    print(f"Saved {len(df)} rows to {path}")


if __name__ == "__main__":
    main()
