"""Stage archived tape days on this machine for analysis and RAG: download a date range's Parquet files from the archive bucket and, if duckdb is installed, build one DuckDB file over them.
Reads only (S3 downloads); touches no database and places nothing. The bucket comes from ARCHIVE_BUCKET in .env, credentials from the AWS CLI login (AWS_PROFILE works).

    python scripts/stage_range.py 2026-10-28 2026-10-31                     # all tape tables into ./staged/
    python scripts/stage_range.py 2026-10-28 2026-10-31 --table quotes --table index_ticks --out ~/stage

Files land as <out>/<table>-<day>.parquet (a day already there is skipped; a day not archived yet is reported and passed over).
With duckdb (`pip install duckdb`), <out>/stage.duckdb holds one view per table over all downloaded days, plus `quotes_with_index`: every quote with the latest
1-second index tick at or before it, for the coin the market settles on (BTC, ETH, SOL, XRP, DOGE 15-minute markets). Open it with `duckdb <out>/stage.duckdb`.
Orders, fills and decisions are not in the archive (they live in the Postgres main schema); use `scripts/cloud.sh sql` for those.
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from kalshi_moonshot import archive, config

INDEX_OF = "CASE WHEN ticker LIKE 'KXBTC%' THEN 'BRTI' WHEN ticker LIKE 'KXETH%' THEN 'ETHUSD_RTI' WHEN ticker LIKE 'KXSOL%' THEN 'SOLUSD_RTI' WHEN ticker LIKE 'KXXRP%' THEN 'XRPUSD_RTI' WHEN ticker LIKE 'KXDOGE%' THEN 'DOGEUSD_RTI' END"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("start", help="first UTC day, YYYY-MM-DD")
    ap.add_argument("end", help="last UTC day, YYYY-MM-DD")
    ap.add_argument("--out", default="staged", help="folder to stage into (default ./staged)")
    ap.add_argument("--table", action="append", choices=list(archive.TABLES), help="only this table (repeatable)")
    args = ap.parse_args()
    s = config.reload()
    if not s.archive_bucket:
        print("ARCHIVE_BUCKET is not set in .env")
        return 1
    first, last = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if last < first:
        print("end is before start")
        return 1
    out, tables = Path(args.out).expanduser(), args.table or list(archive.TABLES)
    client = archive.client_for(s)
    got = missing = 0
    for n in range((last - first).days + 1):
        day = (first + timedelta(days=n)).isoformat()
        for table in tables:
            if (out / f"{table}-{day}.parquet").exists():
                continue
            try:
                archive.fetch_day(s, client, day, out, [table])
                got += 1
            except Exception as exc:  # a day not archived yet (404) or a credentials problem: say so and go on
                missing += 1
                print(f"{table} {day}: {str(exc)[:100]}")
    print(f"downloaded {got} files into {out}, {missing} not available")
    try:
        import duckdb
    except ImportError:
        print("duckdb is not installed: the Parquet files are ready; `pip install duckdb` to also get stage.duckdb")
        return 0
    con = duckdb.connect(str(out / "stage.duckdb"))
    have = [t for t in tables if list(out.glob(f"{t}-*.parquet"))]
    for t in have:
        con.execute(f"CREATE OR REPLACE VIEW {t} AS SELECT * FROM read_parquet('{out}/{t}-*.parquet')")
    if "quotes" in have and "index_ticks" in have:
        con.execute(
            f"CREATE OR REPLACE VIEW quotes_with_index AS SELECT q.*, i.value AS index_value, i.avg60 AS index_avg60, i.quarter_avg AS index_quarter_avg "
            f"FROM (SELECT *, {INDEX_OF} AS index_id FROM quotes) q ASOF LEFT JOIN index_ticks i ON q.index_id = i.index_id AND q.ts >= i.ts"
        )
    con.close()
    print(f"built {out / 'stage.duckdb'} with views: {', '.join(have + (['quotes_with_index'] if 'quotes' in have and 'index_ticks' in have else []))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
