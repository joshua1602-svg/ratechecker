# data/ingest.py
# VOA bulk data ingest — list entries and summary valuations
#
# Usage (from project root):
#   python data/ingest.py <list_entries_file> <summary_valuations_file>
#   Files can be .csv or .zip

import os
import sys
import zipfile
from contextlib import contextmanager
from io import StringIO

# Allow `from db import ...` regardless of working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from sqlalchemy import text

from db import get_engine

engine = get_engine()


# ─────────────────────────────────────────────
# DB WRITE HELPER
# ─────────────────────────────────────────────

def _is_postgres() -> bool:
    url = str(engine.url)
    return "postgresql" in url or "postgres" in url


def _write_df(df: pd.DataFrame, table_name: str, if_exists: str) -> None:
    """
    Write a DataFrame to the database.

    For PostgreSQL/Supabase: uses COPY FROM STDIN, which streams data as CSV
    through the connection and is not subject to statement timeouts.  10-50x
    faster than INSERT and reliable on slow or remote connections.

    For SQLite (local dev): falls back to to_sql with small batches.

    if_exists:
      'replace' — drop the table and recreate it, then load.
      'append'  — table already exists, stream data straight in.
    """
    if _is_postgres():
        if if_exists == "replace":
            # Create (or replace) the table schema using an empty DataFrame.
            # Pandas infers column types from the dtypes; no data is inserted here.
            with engine.connect() as conn:
                df.head(0).to_sql(table_name, conn, if_exists="replace", index=False)
                conn.commit()

        # Stream data via COPY FROM STDIN — no statement timeout, single round-trip.
        buf = StringIO()
        df.to_csv(buf, index=False, header=True, na_rep="")
        buf.seek(0)

        raw = engine.raw_connection()
        try:
            with raw.cursor() as cur:
                cur.copy_expert(
                    f"COPY {table_name} FROM STDIN WITH (FORMAT CSV, HEADER TRUE, NULL '')",
                    buf,
                )
            raw.commit()
        finally:
            raw.close()

    else:
        # SQLite fallback — executemany (method=None) is faster than multi-row INSERT for SQLite
        df.to_sql(
            table_name,
            engine,
            if_exists=if_exists,
            index=False,
            chunksize=1_000,
        )

# ── CRITICAL: VOA files are asterisk-delimited ASCII despite .csv extension ──
VOA_DELIMITER = "*"
VOA_ENCODING = "latin-1"  # pragmatic default; spec is ASCII, latin-1 is a superset

# Rows per read/write cycle — tune down if still hitting memory limits
READ_CHUNK = 50_000   # rows read from CSV at once
FLUSH_EVERY = 50_000  # rows accumulated before flushing to DB (summary valuations)


# ─────────────────────────────────────────────
# FILE HELPERS
# ─────────────────────────────────────────────

@contextmanager
def _open_csv(filepath: str, name_fragment: str):
    """
    Context manager that yields an open file handle for a CSV (or the matching
    member of a zip archive).  Keeping this as a context manager ensures the
    ZipFile stays open for the full duration of chunked reads.
    """
    if filepath.endswith(".zip"):
        with zipfile.ZipFile(filepath) as z:
            candidates = [f for f in z.namelist() if name_fragment in f.lower()]
            if not candidates:
                raise FileNotFoundError(
                    f"No file matching '{name_fragment}' found in {filepath}. "
                    f"Contents: {z.namelist()}"
                )
            with z.open(candidates[0]) as f:
                yield f
    else:
        with open(filepath, "r", encoding=VOA_ENCODING) as f:
            yield f


# ─────────────────────────────────────────────
# LIST ENTRIES
# ─────────────────────────────────────────────

LIST_ENTRY_COLUMNS = [
    "entry_number",               # 1  - incrementing, discard after load
    "ba_code",                    # 2
    "ndr_community_code",         # 3
    "ba_reference_number",        # 4
    "primary_description_code",   # 5
    "primary_description_text",   # 6
    "uarn",                       # 7  - PRIMARY KEY for hereditament
    "full_property_identifier",   # 8
    "firm_name",                  # 9
    "number_or_name",             # 10
    "street",                     # 11
    "town",                       # 12
    "postal_district",            # 13
    "county",                     # 14
    "postcode",                   # 15
    "effective_date",             # 16 - null for reval assessments (see D6c)
    "composite_indicator",        # 17 - 'C' = mixed domestic/non-domestic
    "rateable_value",             # 18 - null for proxy deletion records
    "appeal_settlement_code",     # 19
    "assessment_reference_number",# 20 - secondary key
    "list_alteration_date",       # 21 - null for reval assessments (see D6c)
    "scat_code_and_suffix",       # 22
    "sub_street_level_3",         # 23
    "sub_street_level_2",         # 24
    "sub_street_level_1",         # 25
    "case_number",                # 26
    "current_from_date",          # 27
    "current_to_date",            # 28
]

COLS_TO_STORE = [
    "uarn",
    "assessment_reference_number",
    "ba_code",
    "scat_code",
    "scat_code_and_suffix",
    "primary_description_code",
    "primary_description_text",
    "postcode",
    "postcode_sector",
    "full_property_identifier",
    "street",
    "town",
    "county",
    "rateable_value",
    "effective_date",
    "list_alteration_date",
    "composite_indicator",
    "current_from_date",
    "current_to_date",
]

# SCAT codes for Phase 1 property types (confirmed from VOA spec Appendix 2)
PHASE_1_SCAT_CODES = [249, 251, 409, 234, 417, 85, 416]
PHASE_1_SCAT_SET = set(PHASE_1_SCAT_CODES)


def _process_list_chunk(chunk: pd.DataFrame) -> pd.DataFrame:
    """Apply all filters and type conversions to one chunk of list entries."""
    # Extract numeric SCAT code (strip suffix letter, e.g. "249S" → 249)
    chunk["scat_code"] = pd.to_numeric(
        chunk["scat_code_and_suffix"].str.extract(r"^(\d+)")[0],
        errors="coerce",
    )

    # Filter to Phase 1 SCAT codes
    chunk = chunk[chunk["scat_code"].isin(PHASE_1_SCAT_SET)]
    if chunk.empty:
        return chunk

    # Exclude composite properties
    chunk = chunk[chunk["composite_indicator"].str.strip() != "C"]

    # Exclude proxy deletion records (null rateable_value)
    chunk["rateable_value"] = pd.to_numeric(chunk["rateable_value"], errors="coerce")
    chunk = chunk[chunk["rateable_value"].notna()]

    if chunk.empty:
        return chunk

    # Numeric type conversions
    chunk["uarn"] = pd.to_numeric(chunk["uarn"], errors="coerce")
    chunk["assessment_reference_number"] = pd.to_numeric(
        chunk["assessment_reference_number"], errors="coerce"
    )

    # Postcode normalisation + sector derivation
    chunk["postcode"] = chunk["postcode"].str.strip().str.upper()
    chunk["postcode_sector"] = chunk["postcode"].str.extract(
        r"^([A-Z]{1,2}\d{1,2}[A-Z]?\s\d)"
    )[0]

    # Modelling rule (D4): exclude launderettes from SCAT 249
    launderette_mask = (chunk["scat_code"] == 249) & (
        chunk["primary_description_text"].str.contains("LAUNDERETTE", case=False, na=False)
    )
    chunk = chunk[~launderette_mask]

    return chunk[COLS_TO_STORE]


def ingest_list_entries(filepath: str) -> int:
    """
    Stream VOA compiled list entries into voa_list_entries in chunks.

    - Filters to Phase 1 SCAT codes.
    - Excludes composite properties (composite_indicator = 'C').
    - Excludes proxy deletion records (rateable_value IS NULL).
    - Excludes launderettes from SCAT 249.
    - Accepts null effective_date and list_alteration_date (see D6c).

    Returns total rows written.
    """
    print(f"Reading list entries from {filepath} (chunk size: {READ_CHUNK:,})…")

    rows_written = 0
    chunks_read = 0
    first_write = True

    with _open_csv(filepath, "listentries") as f:
        reader = pd.read_csv(
            f,
            sep=VOA_DELIMITER,
            encoding=VOA_ENCODING,
            header=None,
            names=LIST_ENTRY_COLUMNS,
            dtype=str,
            chunksize=READ_CHUNK,
        )

        for chunk in reader:
            chunks_read += 1

            # Validate delimiter on the very first chunk
            if chunks_read == 1 and len(chunk.columns) < 10:
                raise ValueError(
                    f"First chunk has only {len(chunk.columns)} columns — "
                    "check the delimiter is '*' not ','."
                )

            processed = _process_list_chunk(chunk)
            if processed.empty:
                continue

            _write_df(processed, "voa_list_entries", "replace" if first_write else "append")
            rows_written += len(processed)
            first_write = False

            if chunks_read % 10 == 0:
                print(f"  … {chunks_read * READ_CHUNK:,} rows read, {rows_written:,} written so far")

    print(f"Done. {rows_written:,} rows written to voa_list_entries.")
    return rows_written


# ─────────────────────────────────────────────
# SUMMARY VALUATIONS
# ─────────────────────────────────────────────

SV_HEADER_COLUMNS = [
    "record_type",                # 1  - always '01'
    "assessment_reference_number",# 2
    "uarn",                       # 3
    "ba_code",                    # 4
    "firm_name",                  # 5
    "number_or_name",             # 6
    "sub_street_level_3",         # 7
    "sub_street_level_2",         # 8
    "sub_street_level_1",         # 9
    "street",                     # 10
    "postal_district",            # 11
    "town",                       # 12
    "county",                     # 13
    "postcode",                   # 14
    "scheme_reference",           # 15
    "primary_description_text",   # 16
    "total_area_or_units",        # 17 - area (m²) OR unit count; ambiguous (see D6b)
    "sub_total",                  # 18
    "total_value",                # 19
    "adopted_rv",                 # 20
    "list_year",                  # 21
    "ba_name",                    # 22
    "ba_reference_number",        # 23
    "vo_ref",                     # 24
    "from_date",                  # 25
    "to_date",                    # 26
    "scat_code",                  # 27 - 3-char code only (no suffix)
    "unit_of_measurement",        # 28 - NIA, GIA, EFA, GEA, RCA, OTH (see D6a)
    "unadjusted_price_psm",       # 29 - VOA primary survey unit rate (£/m²)
]

SV_LINE_COLUMNS = [
    "record_type",  # 1  - always '02'
    "line_number",  # 2
    "floor",        # 3
    "description",  # 4
    "area",         # 5
    "price",        # 6
    "value",        # 7
]


def _flush_headers(batch: list[dict], first: bool) -> None:
    df = pd.DataFrame(batch)
    df["uarn"] = pd.to_numeric(df["uarn"], errors="coerce")
    df["assessment_reference_number"] = pd.to_numeric(
        df["assessment_reference_number"], errors="coerce"
    )
    df["unadjusted_price_psm"] = pd.to_numeric(df["unadjusted_price_psm"], errors="coerce")
    df["total_area_or_units"] = pd.to_numeric(df["total_area_or_units"], errors="coerce")
    df["adopted_rv"] = pd.to_numeric(df["adopted_rv"], errors="coerce")
    df["is_nia"] = df["unit_of_measurement"].str.strip() == "NIA"
    _write_df(df, "voa_sv_header", "replace" if first else "append")


def _flush_lines(batch: list[dict], first: bool) -> None:
    df = pd.DataFrame(batch)
    df["uarn"] = pd.to_numeric(df["uarn"], errors="coerce")
    df["area"] = pd.to_numeric(df["area"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    _write_df(df, "voa_sv_lines", "replace" if first else "append")


def ingest_summary_valuations(filepath: str) -> tuple[int, int]:
    """
    Stream the multi-record-type summary valuation file into voa_sv_header and
    voa_sv_lines, flushing to the DB every FLUSH_EVERY records so neither table
    accumulates in memory.

    Returns (headers_total, lines_total).
    """
    print(f"Parsing summary valuations from {filepath} (flush every {FLUSH_EVERY:,})…")

    headers_batch: list[dict] = []
    lines_batch: list[dict] = []
    headers_total = 0
    lines_total = 0
    first_headers = True
    first_lines = True
    current_uarn = None
    current_assessment_ref = None

    def _maybe_flush_headers(force: bool = False) -> None:
        nonlocal headers_batch, headers_total, first_headers
        if headers_batch and (force or len(headers_batch) >= FLUSH_EVERY):
            _flush_headers(headers_batch, first_headers)
            headers_total += len(headers_batch)
            headers_batch = []
            first_headers = False
            print(f"  … {headers_total:,} header rows written")

    def _maybe_flush_lines(force: bool = False) -> None:
        nonlocal lines_batch, lines_total, first_lines
        if lines_batch and (force or len(lines_batch) >= FLUSH_EVERY):
            _flush_lines(lines_batch, first_lines)
            lines_total += len(lines_batch)
            lines_batch = []
            first_lines = False
            print(f"  … {lines_total:,} line rows written")

    if filepath.endswith(".zip"):
        with zipfile.ZipFile(filepath) as z:
            candidates = [f for f in z.namelist() if "summaryvaluations" in f.lower()]
            if not candidates:
                raise FileNotFoundError(
                    f"No summary valuations file found in {filepath}. "
                    f"Contents: {z.namelist()}"
                )
            raw_file = z.open(candidates[0])
    else:
        raw_file = open(filepath, "r", encoding=VOA_ENCODING)

    for raw_line in raw_file:
        if isinstance(raw_line, bytes):
            raw_line = raw_line.decode(VOA_ENCODING)
        line = raw_line.strip()
        if not line:
            continue

        fields = line.split("*")
        record_type = fields[0].strip()

        if record_type == "01":
            padded = fields + [""] * (len(SV_HEADER_COLUMNS) - len(fields))
            row = dict(zip(SV_HEADER_COLUMNS, padded[: len(SV_HEADER_COLUMNS)]))
            current_uarn = row["uarn"]
            current_assessment_ref = row["assessment_reference_number"]
            headers_batch.append(row)
            _maybe_flush_headers()

        elif record_type == "02":
            padded = fields + [""] * (len(SV_LINE_COLUMNS) - len(fields))
            row = dict(zip(SV_LINE_COLUMNS, padded[: len(SV_LINE_COLUMNS)]))
            row["uarn"] = current_uarn
            row["assessment_reference_number"] = current_assessment_ref
            lines_batch.append(row)
            _maybe_flush_lines()

        # Record types 03–07: not needed for Phase 1 — skip

    raw_file.close()

    # Final flush of any remaining rows
    _maybe_flush_headers(force=True)
    _maybe_flush_lines(force=True)

    print(f"Done. {headers_total:,} headers and {lines_total:,} lines written.")
    return headers_total, lines_total


# ─────────────────────────────────────────────
# POSTCODE GEOCODING
# ─────────────────────────────────────────────

def geocode_postcodes() -> None:
    """
    Populate postcode_coords with lat/lon for every unique postcode in
    voa_list_entries, using the postcodes.io bulk API (100 per request).

    This is the reference table used by api/db.py for spatial queries.
    Run once after ingest_list_entries().
    """
    import time
    import httpx

    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT DISTINCT postcode FROM voa_list_entries WHERE postcode IS NOT NULL")
        )
        postcodes = [r[0] for r in result.fetchall()]

    print(f"Geocoding {len(postcodes):,} unique postcodes via postcodes.io…")

    coords: list[dict] = []
    batch_size = 100

    for i in range(0, len(postcodes), batch_size):
        batch = postcodes[i : i + batch_size]
        try:
            resp = httpx.post(
                "https://api.postcodes.io/postcodes",
                json={"postcodes": batch},
                timeout=30.0,
            )
            for item in resp.json().get("result", []):
                if item and item.get("result"):
                    r = item["result"]
                    coords.append(
                        {
                            "postcode": r["postcode"],
                            "latitude": r["latitude"],
                            "longitude": r["longitude"],
                        }
                    )
        except Exception as exc:
            print(f"  Batch {i // batch_size + 1} failed: {exc} — skipping")

        if (i // batch_size) % 100 == 0 and i > 0:
            print(f"  Progress: {i:,} / {len(postcodes):,} postcodes geocoded…")
            time.sleep(0.5)  # be polite to the free API

    df_coords = pd.DataFrame(coords)
    _write_df(df_coords, "postcode_coords", "replace")

    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_pc_postcode "
                "ON postcode_coords(postcode)"
            )
        )
        conn.commit()

    print(f"Geocoded {len(df_coords):,} postcodes into postcode_coords.")


# ─────────────────────────────────────────────
# POST-INGEST: JOIN AND VALIDATE
# ─────────────────────────────────────────────

def add_summary_valuation_flag() -> None:
    """Add has_summary_valuation boolean to voa_list_entries."""
    print("Adding has_summary_valuation flag…")
    with engine.connect() as conn:
        conn.execute(
            text("""
                ALTER TABLE voa_list_entries
                ADD COLUMN IF NOT EXISTS has_summary_valuation BOOLEAN DEFAULT FALSE;
            """)
        )
        conn.execute(
            text("""
                UPDATE voa_list_entries le
                SET has_summary_valuation = TRUE
                WHERE EXISTS (
                    SELECT 1 FROM voa_sv_header sv
                    WHERE sv.uarn = le.uarn
                );
            """)
        )
        conn.commit()

    with engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN has_summary_valuation THEN 1 ELSE 0 END) AS with_sv,
                    ROUND(
                        100.0 * SUM(CASE WHEN has_summary_valuation THEN 1 ELSE 0 END) / COUNT(*),
                        1
                    ) AS pct
                FROM voa_list_entries
            """)
        )
        row = result.fetchone()
        print(
            f"Summary valuation coverage: {row.with_sv:,} / {row.total:,} ({row.pct}%)"
        )
        assert 60 <= float(row.pct) <= 95, (
            f"Coverage {row.pct}% outside expected 60–95% range — check the uarn join"
        )


def create_indexes() -> None:
    """Create indexes after data load for query performance."""
    print("Creating indexes…")
    with engine.connect() as conn:
        for stmt in [
            "CREATE INDEX IF NOT EXISTS idx_le_postcode_sector ON voa_list_entries(postcode_sector)",
            "CREATE INDEX IF NOT EXISTS idx_le_scat_code ON voa_list_entries(scat_code)",
            "CREATE INDEX IF NOT EXISTS idx_le_uarn ON voa_list_entries(uarn)",
            "CREATE INDEX IF NOT EXISTS idx_sv_uarn ON voa_sv_header(uarn)",
            "CREATE INDEX IF NOT EXISTS idx_svl_uarn ON voa_sv_lines(uarn)",
        ]:
            conn.execute(text(stmt))
        conn.commit()
    print("Indexes created.")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

USAGE = """
VOA ingest pipeline — run each step independently or all at once.

Subcommands:
  list    <list_file>             Ingest list entries only
  sv      <sv_file>               Ingest summary valuations only
  post                            Add has_summary_valuation flag + indexes
  geocode                         Geocode postcodes into postcode_coords
  all     <list_file> <sv_file>   Run every step in order

Examples:
  python data/ingest.py list    listentries.csv
  python data/ingest.py sv      summaryvaluations.csv
  python data/ingest.py post
  python data/ingest.py geocode
  python data/ingest.py all     listentries.csv summaryvaluations.csv

Files can be raw .csv or .zip archives.
"""

if __name__ == "__main__":
    args = sys.argv[1:]

    if not args:
        print(USAGE)
        sys.exit(1)

    cmd = args[0].lower()

    if cmd == "list":
        if len(args) < 2:
            print("Usage: python data/ingest.py list <list_file>")
            sys.exit(1)
        ingest_list_entries(args[1])

    elif cmd == "sv":
        if len(args) < 2:
            print("Usage: python data/ingest.py sv <sv_file>")
            sys.exit(1)
        ingest_summary_valuations(args[1])

    elif cmd == "post":
        add_summary_valuation_flag()
        create_indexes()

    elif cmd == "geocode":
        geocode_postcodes()

    elif cmd == "all":
        if len(args) < 3:
            print("Usage: python data/ingest.py all <list_file> <sv_file>")
            sys.exit(1)
        ingest_list_entries(args[1])
        ingest_summary_valuations(args[2])
        add_summary_valuation_flag()
        geocode_postcodes()
        create_indexes()

    else:
        print(f"Unknown subcommand: {cmd!r}")
        print(USAGE)
        sys.exit(1)

    print("\nDone.")
