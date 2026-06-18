"""
Pulls resolved Polymarket events + price history into backtest.db.
Resumable — skips events already collected.

Price history is fetched concurrently (up to CONCURRENCY requests at once)
using asyncio + aiohttp, making collection ~10-20x faster than sequential.
"""

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime

import ssl

import aiohttp
import certifi
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

DB_PATH     = "backtest.db"
GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
CONCURRENCY = 20   # concurrent price-history requests
RETRIES     = 4

SERIES = {
    "ATP": 10365,
    "WTA": 10366,
    "ITF": 11634,
    "MLB": 3,
    "UFC": 38,
}

# ── schema ────────────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id           TEXT PRIMARY KEY,
            title        TEXT,
            series_id    INTEGER,
            sport        TEXT,
            start_date   TEXT,
            end_date     TEXT,
            collected_at TEXT
        );

        CREATE TABLE IF NOT EXISTS markets (
            id               TEXT PRIMARY KEY,
            event_id         TEXT,
            question         TEXT,
            yes_token_id     TEXT,
            resolved_outcome INTEGER,
            volume           REAL,
            collected_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS price_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            timestamp INTEGER,
            price     REAL,
            UNIQUE(market_id, timestamp)
        );

        CREATE INDEX IF NOT EXISTS idx_ph_market  ON price_history(market_id);
        CREATE INDEX IF NOT EXISTS idx_mkt_event  ON markets(event_id);
        CREATE INDEX IF NOT EXISTS idx_evt_series ON events(series_id);
    """)
    conn.commit()


# ── sync helpers (event pagination) ──────────────────────────────────────────

def _get(url: str, params: dict) -> dict | list:
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if attempt == RETRIES - 1:
                raise
            time.sleep(1.5 ** attempt)


def fetch_events(series_id: int, page_size: int = 50):
    offset = 0
    while True:
        events = _get(
            f"{GAMMA_API}/events",
            {"series_id": series_id, "closed": "true",
             "limit": page_size, "offset": offset,
             "order": "startDate", "ascending": "false"},
        )
        if not events:
            break
        yield from events
        if len(events) < page_size:
            break
        offset += page_size
        time.sleep(0.2)


# ── async price-history fetching ──────────────────────────────────────────────

async def _fetch_one(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                     token_id: str) -> list[dict]:
    url = f"{CLOB_API}/prices-history"
    params = {"market": token_id, "interval": "all", "fidelity": 60}
    async with sem:
        for attempt in range(RETRIES):
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 429:
                        wait = 2 ** attempt
                        logging.warning(f"Rate limited — sleeping {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    return data.get("history", [])
            except Exception as exc:
                if attempt == RETRIES - 1:
                    logging.warning(f"price_history failed {token_id[:12]}: {exc}")
                    return []
                await asyncio.sleep(1.5 ** attempt)
    return []


async def fetch_prices_batch(markets: list[tuple]) -> dict[str, list]:
    """
    markets: list of (market_id, token_id)
    Returns {market_id: [price_rows]} where price_rows are (market_id, ts, price).
    """
    sem = asyncio.Semaphore(CONCURRENCY)
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(limit=CONCURRENCY, ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = {
            mkt_id: asyncio.create_task(_fetch_one(session, sem, token_id))
            for mkt_id, token_id in markets
        }
        results = {}
        for mkt_id, task in tasks.items():
            history = await task
            rows = [
                (mkt_id, int(h["t"]), float(h["p"]))
                for h in history
                if "t" in h and "p" in h
            ]
            results[mkt_id] = rows
    return results


# ── parsing helpers ───────────────────────────────────────────────────────────

def _parse_json_field(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return []
    return []


def _resolved_outcome(outcome_prices: list) -> int | None:
    if len(outcome_prices) < 2:
        return None
    try:
        yes = float(outcome_prices[0])
        if yes >= 0.99:
            return 1
        if yes <= 0.01:
            return 0
    except (TypeError, ValueError):
        pass
    return None


# ── main collection logic ─────────────────────────────────────────────────────

def collect_sport(conn: sqlite3.Connection, sport: str, series_id: int) -> None:
    cur = conn.cursor()
    collected = skipped = 0
    now = datetime.utcnow().isoformat()

    # Batch markets needing price history so we can fetch concurrently
    BATCH = 200   # fetch this many markets' price histories at once

    pending_markets: list[tuple] = []   # (mkt_id, token_id, title)

    def flush_batch():
        if not pending_markets:
            return
        to_fetch = [(m[0], m[1]) for m in pending_markets]
        results  = asyncio.run(fetch_prices_batch(to_fetch))
        total_pts = 0
        for mkt_id, token_id, title in pending_markets:
            rows = results.get(mkt_id, [])
            if rows:
                cur.executemany(
                    "INSERT OR IGNORE INTO price_history (market_id, timestamp, price) VALUES (?,?,?)",
                    rows,
                )
                total_pts += len(rows)
        conn.commit()
        logging.info(f"  [{sport}] flushed batch of {len(pending_markets)} markets — {total_pts} price pts")
        pending_markets.clear()

    for event in fetch_events(series_id):
        event_id = event.get("id")
        if not event_id:
            continue

        if cur.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone():
            skipped += 1
            continue

        title      = event.get("title", "")
        start_date = event.get("startDate", "")
        end_date   = event.get("endDate", "")

        cur.execute(
            "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)",
            (event_id, title, series_id, sport, start_date, end_date, now),
        )

        for mkt in event.get("markets", []):
            mkt_id = mkt.get("conditionId") or mkt.get("id")
            if not mkt_id:
                continue

            question   = mkt.get("question", "")
            volume     = float(mkt.get("volume") or 0)
            token_ids  = _parse_json_field(mkt.get("clobTokenIds", []))
            yes_token  = token_ids[0] if token_ids else None
            out_prices = _parse_json_field(mkt.get("outcomePrices", []))
            outcome    = _resolved_outcome(out_prices)

            cur.execute(
                "INSERT OR IGNORE INTO markets VALUES (?,?,?,?,?,?,?)",
                (mkt_id, event_id, question, yes_token, outcome, volume, now),
            )

            if yes_token and outcome is not None:
                pending_markets.append((mkt_id, yes_token, title))

        collected += 1

        if len(pending_markets) >= BATCH:
            flush_batch()

    flush_batch()  # remainder
    conn.commit()
    logging.info(f"[{sport}] collected={collected} skipped={skipped}")


# ── backfill missing price histories ─────────────────────────────────────────

def backfill_missing(conn: sqlite3.Connection, sport: str | None = None) -> None:
    """
    Find all markets that have a resolved outcome + yes_token but no price
    history, then fetch their histories concurrently in batches.
    Useful after a partial collection run.
    """
    sport_filter = f"AND e.sport = '{sport}'" if sport else ""
    rows = conn.execute(f"""
        SELECT m.id, m.yes_token_id, e.sport, e.title
        FROM markets m
        JOIN events e ON m.event_id = e.id
        WHERE m.resolved_outcome IS NOT NULL
          AND m.yes_token_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM price_history ph WHERE ph.market_id = m.id
          )
          {sport_filter}
        ORDER BY e.start_date
    """).fetchall()

    total = len(rows)
    logging.info(f"Backfilling {total:,} markets with missing price history "
                 f"({'all sports' if not sport else sport}) …")

    BATCH = 200
    done = 0
    cur = conn.cursor()

    for batch_start in range(0, total, BATCH):
        batch = rows[batch_start: batch_start + BATCH]
        to_fetch = [(r[0], r[1]) for r in batch]
        results  = asyncio.run(fetch_prices_batch(to_fetch))

        total_pts = 0
        for mkt_id, token_id, sport_name, title in batch:
            price_rows = results.get(mkt_id, [])
            if price_rows:
                cur.executemany(
                    "INSERT OR IGNORE INTO price_history (market_id, timestamp, price) VALUES (?,?,?)",
                    price_rows,
                )
                total_pts += len(price_rows)
        conn.commit()

        done += len(batch)
        logging.info(f"  backfill {done:,}/{total:,} — batch {total_pts:,} pts")


# ── entry point ───────────────────────────────────────────────────────────────

def main(backfill: bool = False, sport: str | None = None) -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if backfill:
        backfill_missing(conn, sport=sport)
    else:
        for s, series_id in SERIES.items():
            logging.info(f"── {s} (series_id={series_id}) ──")
            try:
                collect_sport(conn, s, series_id)
            except Exception as exc:
                logging.error(f"Failed {s}: {exc}")

    conn.close()
    logging.info("Done.")


if __name__ == "__main__":
    import sys
    # Usage: python3 data_collector.py [backfill [sport]]
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        sport_arg = sys.argv[2].upper() if len(sys.argv) > 2 else None
        main(backfill=True, sport=sport_arg)
    else:
        main()
