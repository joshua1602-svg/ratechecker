# data/ingest.py
# VOA bulk data ingest — list entries and summary valuations
#
# Usage (from project root):
#   python data/ingest.py <list_entries_file> <summary_valuations_file>
#   Files can be .csv or .zip

import os
import sys
import time
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
        # PostgreSQL COPY is atomic: if the connection drops mid-transfer the server
        # rolls back the entire batch, so retrying is safe.
        buf = StringIO()
        df.to_csv(buf, index=False, header=True, na_rep="")

        last_exc: Exception | None = None
        for attempt in range(1, 4):
            buf.seek(0)
            raw = engine.raw_connection()
            try:
                with raw.cursor() as cur:
                    cur.copy_expert(
                        f"COPY {table_name} FROM STDIN WITH (FORMAT CSV, HEADER TRUE, NULL '')",
                        buf,
                    )
                raw.commit()
                raw.close()
                break  # success — exit retry loop
            except Exception as exc:
                last_exc = exc
                try:
                    raw.close()
                except Exception:
                    pass
                wait = 2 ** attempt  # 2s, 4s, 8s
                print(f"  ! COPY attempt {attempt}/3 failed: {exc}")
                if attempt < 3:
                    print(f"    Retrying in {wait}s with a fresh connection…")
                    time.sleep(wait)
        else:
            raise RuntimeError(
                f"COPY to {table_name} failed after 3 attempts"
            ) from last_exc

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
READ_CHUNK = 50_000        # rows read from CSV at once
FLUSH_HEADERS = 50_000    # header rows per COPY batch (29 columns, moderate row width)
FLUSH_LINES   = 20_000    # line rows per COPY batch — smaller so a dropped connection
                          # mid-COPY has less work to retry

LIST_ENTRY_FIELD_COUNT = 28


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


def _validate_list_entry_field_count(filepath: str) -> None:
    with _open_csv(filepath, "listentries") as f:
        first_line = f.readline()
        if isinstance(first_line, bytes):
            first_line = first_line.decode(VOA_ENCODING)
        if not first_line:
            raise ValueError("Compiled list entries file is empty.")
        field_count = len(first_line.rstrip("\r\n").split(VOA_DELIMITER))
        if field_count != LIST_ENTRY_FIELD_COUNT:
            raise ValueError(
                f"Compiled list entries must have exactly {LIST_ENTRY_FIELD_COUNT} fields; "
                f"found {field_count}."
            )


def _validate_row_field_count(fields: list[str], expected: int, record_type: str) -> None:
    if len(fields) > expected:
        raise ValueError(
            f"Summary valuation record type {record_type} has {len(fields)} fields; "
            f"expected at most {expected}."
        )


def _build_sv_row(
    fields: list[str],
    columns: list[str],
    record_type: str,
    current_uarn: str | None = None,
    current_assessment_ref: str | None = None,
) -> dict:
    _validate_row_field_count(fields, len(columns), record_type)
    padded = fields + [""] * (len(columns) - len(fields))
    row = dict(zip(columns, padded[: len(columns)]))
    if record_type != "01":
        if current_uarn is None or current_assessment_ref is None:
            raise ValueError(
                f"Summary valuation record type {record_type} encountered before a parent "
                "record type 01 established uarn and assessment_reference_number."
            )
        row["uarn"] = current_uarn
        row["assessment_reference_number"] = current_assessment_ref
    return row


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
    chunk["postcode"] = chunk["postcode"].astype(str).str.strip().str.upper()
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

    _validate_list_entry_field_count(filepath)

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

            # Validate delimiter and compiled list structure on the very first chunk
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
    "uarn",
    "assessment_reference_number",
]

SV_ADDITIONS_COLUMNS = [
    "record_type",
    "oa_description",
    "oa_size",
    "oa_price",
    "oa_value",
    "uarn",
    "assessment_reference_number",
]

SV_PLANT_MACHINERY_COLUMNS = [
    "record_type",
    "pm_value",
    "uarn",
    "assessment_reference_number",
]

SV_CAR_PARKING_COLUMNS = [
    "record_type",
    "cp_spaces",
    "cp_spaces_value",
    "cp_area",
    "cp_area_value",
    "cp_total",
    "uarn",
    "assessment_reference_number",
]

SV_ADJUSTMENTS_COLUMNS = [
    "record_type",
    "adj_desc",
    "adj_percent",
    "uarn",
    "assessment_reference_number",
]

SV_ADJUSTMENT_TOTALS_COLUMNS = [
    "record_type",
    "total_before_adj",
    "total_adj",
    "uarn",
    "assessment_reference_number",
]


def _flush_headers(batch: list[dict], first: bool) -> None:
    df = pd.DataFrame(batch)
    df["uarn"] = pd.to_numeric(df["uarn"], errors="coerce")
    df["assessment_reference_number"] = pd.to_numeric(
        df["assessment_reference_number"], errors="coerce"
    )
    df["scat_code"] = pd.to_numeric(df["scat_code"], errors="coerce")
    df["total_area_or_units"] = pd.to_numeric(df["total_area_or_units"], errors="coerce")
    df["sub_total"] = pd.to_numeric(df["sub_total"], errors="coerce")
    df["total_value"] = pd.to_numeric(df["total_value"], errors="coerce")
    df["adopted_rv"] = pd.to_numeric(df["adopted_rv"], errors="coerce")
    df["unit_of_measurement"] = df["unit_of_measurement"].astype(str).str.strip().str.upper()
    df["unadjusted_price_psm"] = pd.to_numeric(df["unadjusted_price_psm"], errors="coerce")
    df["is_nia"] = df["unit_of_measurement"] == "NIA"
    _write_df(df, "voa_sv_header", "replace" if first else "append")


def _flush_lines(batch: list[dict], first: bool) -> None:
    df = pd.DataFrame(batch)
    df["uarn"] = pd.to_numeric(df["uarn"], errors="coerce")
    df["assessment_reference_number"] = pd.to_numeric(
        df["assessment_reference_number"], errors="coerce"
    )
    df["line_number"] = pd.to_numeric(df["line_number"], errors="coerce")
    df["area"] = pd.to_numeric(df["area"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    _write_df(df, "voa_sv_lines", "replace" if first else "append")


def _flush_additions(batch: list[dict], first: bool) -> None:
    df = pd.DataFrame(batch)
    df["uarn"] = pd.to_numeric(df["uarn"], errors="coerce")
    df["assessment_reference_number"] = pd.to_numeric(df["assessment_reference_number"], errors="coerce")
    df["oa_size"] = pd.to_numeric(df["oa_size"], errors="coerce")
    df["oa_price"] = pd.to_numeric(df["oa_price"], errors="coerce")
    df["oa_value"] = pd.to_numeric(df["oa_value"], errors="coerce")
    _write_df(df, "voa_sv_additions", "replace" if first else "append")


def _flush_plant_machinery(batch: list[dict], first: bool) -> None:
    df = pd.DataFrame(batch)
    df["uarn"] = pd.to_numeric(df["uarn"], errors="coerce")
    df["assessment_reference_number"] = pd.to_numeric(df["assessment_reference_number"], errors="coerce")
    df["pm_value"] = pd.to_numeric(df["pm_value"], errors="coerce")
    _write_df(df, "voa_sv_plant_machinery", "replace" if first else "append")


def _flush_car_parking(batch: list[dict], first: bool) -> None:
    df = pd.DataFrame(batch)
    df["uarn"] = pd.to_numeric(df["uarn"], errors="coerce")
    df["assessment_reference_number"] = pd.to_numeric(df["assessment_reference_number"], errors="coerce")
    df["cp_spaces"] = pd.to_numeric(df["cp_spaces"], errors="coerce")
    df["cp_spaces_value"] = pd.to_numeric(df["cp_spaces_value"], errors="coerce")
    df["cp_area"] = pd.to_numeric(df["cp_area"], errors="coerce")
    df["cp_area_value"] = pd.to_numeric(df["cp_area_value"], errors="coerce")
    df["cp_total"] = pd.to_numeric(df["cp_total"], errors="coerce")
    _write_df(df, "voa_sv_car_parking", "replace" if first else "append")


def _flush_adjustments(batch: list[dict], first: bool) -> None:
    df = pd.DataFrame(batch)
    df["uarn"] = pd.to_numeric(df["uarn"], errors="coerce")
    df["assessment_reference_number"] = pd.to_numeric(df["assessment_reference_number"], errors="coerce")
    df["adj_desc"] = df["adj_desc"].astype(str).str.strip()
    df["adj_percent"] = pd.to_numeric(
        df["adj_percent"].astype(str).str.strip().str.removesuffix("%"),
        errors="coerce",
    )
    _write_df(df, "voa_sv_adjustments", "replace" if first else "append")


def _flush_adjustment_totals(batch: list[dict], first: bool) -> None:
    df = pd.DataFrame(batch)
    df["uarn"] = pd.to_numeric(df["uarn"], errors="coerce")
    df["assessment_reference_number"] = pd.to_numeric(df["assessment_reference_number"], errors="coerce")
    df["total_before_adj"] = pd.to_numeric(df["total_before_adj"], errors="coerce")
    df["total_adj"] = pd.to_numeric(df["total_adj"], errors="coerce")
    _write_df(df, "voa_sv_adjustment_totals", "replace" if first else "append")


def ingest_summary_valuations(filepath: str) -> tuple[int, int]:
    """
    Stream the multi-record-type summary valuation file into voa_sv_header,
    voa_sv_lines, and record types 03–07 child tables while flushing to the DB
    in batches so no table accumulates unbounded in memory.

    Returns (headers_total, lines_total).
    """
    print(f"Parsing summary valuations from {filepath} (headers flush={FLUSH_HEADERS:,}, lines flush={FLUSH_LINES:,})…")

    headers_batch: list[dict] = []
    lines_batch: list[dict] = []
    additions_batch: list[dict] = []
    plant_machinery_batch: list[dict] = []
    car_parking_batch: list[dict] = []
    adjustments_batch: list[dict] = []
    adjustment_totals_batch: list[dict] = []
    headers_total = 0
    lines_total = 0
    additions_total = 0
    plant_machinery_total = 0
    car_parking_total = 0
    adjustments_total = 0
    adjustment_totals_total = 0
    first_headers = True
    first_lines = True
    first_additions = True
    first_plant_machinery = True
    first_car_parking = True
    first_adjustments = True
    first_adjustment_totals = True
    current_uarn = None
    current_assessment_ref = None

    def _maybe_flush_headers(force: bool = False) -> None:
        nonlocal headers_batch, headers_total, first_headers
        if headers_batch and (force or len(headers_batch) >= FLUSH_HEADERS):
            _flush_headers(headers_batch, first_headers)
            headers_total += len(headers_batch)
            headers_batch = []
            first_headers = False
            print(f"  … {headers_total:,} header rows written")

    def _maybe_flush_lines(force: bool = False) -> None:
        nonlocal lines_batch, lines_total, first_lines
        if lines_batch and (force or len(lines_batch) >= FLUSH_LINES):
            _flush_lines(lines_batch, first_lines)
            lines_total += len(lines_batch)
            lines_batch = []
            first_lines = False
            print(f"  … {lines_total:,} line rows written")

    def _maybe_flush_additions(force: bool = False) -> None:
        nonlocal additions_batch, additions_total, first_additions
        if additions_batch and (force or len(additions_batch) >= FLUSH_LINES):
            _flush_additions(additions_batch, first_additions)
            additions_total += len(additions_batch)
            additions_batch = []
            first_additions = False
            print(f"  … {additions_total:,} addition rows written")

    def _maybe_flush_plant_machinery(force: bool = False) -> None:
        nonlocal plant_machinery_batch, plant_machinery_total, first_plant_machinery
        if plant_machinery_batch and (force or len(plant_machinery_batch) >= FLUSH_LINES):
            _flush_plant_machinery(plant_machinery_batch, first_plant_machinery)
            plant_machinery_total += len(plant_machinery_batch)
            plant_machinery_batch = []
            first_plant_machinery = False
            print(f"  … {plant_machinery_total:,} plant/machinery rows written")

    def _maybe_flush_car_parking(force: bool = False) -> None:
        nonlocal car_parking_batch, car_parking_total, first_car_parking
        if car_parking_batch and (force or len(car_parking_batch) >= FLUSH_LINES):
            _flush_car_parking(car_parking_batch, first_car_parking)
            car_parking_total += len(car_parking_batch)
            car_parking_batch = []
            first_car_parking = False
            print(f"  … {car_parking_total:,} car parking rows written")

    def _maybe_flush_adjustments(force: bool = False) -> None:
        nonlocal adjustments_batch, adjustments_total, first_adjustments
        if adjustments_batch and (force or len(adjustments_batch) >= FLUSH_LINES):
            _flush_adjustments(adjustments_batch, first_adjustments)
            adjustments_total += len(adjustments_batch)
            adjustments_batch = []
            first_adjustments = False
            print(f"  … {adjustments_total:,} adjustment rows written")

    def _maybe_flush_adjustment_totals(force: bool = False) -> None:
        nonlocal adjustment_totals_batch, adjustment_totals_total, first_adjustment_totals
        if adjustment_totals_batch and (force or len(adjustment_totals_batch) >= FLUSH_LINES):
            _flush_adjustment_totals(adjustment_totals_batch, first_adjustment_totals)
            adjustment_totals_total += len(adjustment_totals_batch)
            adjustment_totals_batch = []
            first_adjustment_totals = False
            print(f"  … {adjustment_totals_total:,} adjustment total rows written")

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
            row = _build_sv_row(fields, SV_HEADER_COLUMNS, record_type)
            current_uarn = row["uarn"]
            current_assessment_ref = row["assessment_reference_number"]
            headers_batch.append(row)
            _maybe_flush_headers()

        elif record_type == "02":
            lines_batch.append(
                _build_sv_row(fields, SV_LINE_COLUMNS[:-2], record_type, current_uarn, current_assessment_ref)
            )
            _maybe_flush_lines()

        elif record_type == "03":
            additions_batch.append(
                _build_sv_row(fields, SV_ADDITIONS_COLUMNS[:-2], record_type, current_uarn, current_assessment_ref)
            )
            _maybe_flush_additions()

        elif record_type == "04":
            plant_machinery_batch.append(
                _build_sv_row(fields, SV_PLANT_MACHINERY_COLUMNS[:-2], record_type, current_uarn, current_assessment_ref)
            )
            _maybe_flush_plant_machinery()

        elif record_type == "05":
            car_parking_batch.append(
                _build_sv_row(fields, SV_CAR_PARKING_COLUMNS[:-2], record_type, current_uarn, current_assessment_ref)
            )
            _maybe_flush_car_parking()

        elif record_type == "06":
            adjustments_batch.append(
                _build_sv_row(fields, SV_ADJUSTMENTS_COLUMNS[:-2], record_type, current_uarn, current_assessment_ref)
            )
            _maybe_flush_adjustments()

        elif record_type == "07":
            adjustment_totals_batch.append(
                _build_sv_row(fields, SV_ADJUSTMENT_TOTALS_COLUMNS[:-2], record_type, current_uarn, current_assessment_ref)
            )
            _maybe_flush_adjustment_totals()

        else:
            raise ValueError(f"Unsupported summary valuation record type: {record_type!r}")

    raw_file.close()

    # Final flush of any remaining rows
    _maybe_flush_headers(force=True)
    _maybe_flush_lines(force=True)
    _maybe_flush_additions(force=True)
    _maybe_flush_plant_machinery(force=True)
    _maybe_flush_car_parking(force=True)
    _maybe_flush_adjustments(force=True)
    _maybe_flush_adjustment_totals(force=True)

    print(
        "Done. "
        f"{headers_total:,} headers, {lines_total:,} lines, {additions_total:,} additions, "
        f"{plant_machinery_total:,} plant/machinery, {car_parking_total:,} car parking, "
        f"{adjustments_total:,} adjustments, and {adjustment_totals_total:,} adjustment totals written."
    )
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

    # Use a persistent client so one TCP/SSL connection is reused across all
    # batches.  Creating a new SSL handshake per request (httpx.post()) caused
    # frequent "Server disconnected" failures on Windows with 1,200+ batches.
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
        for i in range(0, len(postcodes), batch_size):
            batch = postcodes[i : i + batch_size]
            batch_num = i // batch_size + 1

            # Retry up to 3 times with exponential backoff before skipping
            for attempt in range(1, 4):
                try:
                    resp = client.post(
                        "https://api.postcodes.io/postcodes",
                        json={"postcodes": batch},
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
                    break  # success
                except Exception as exc:
                    if attempt == 3:
                        print(f"  Batch {batch_num} failed after 3 attempts: {exc} — skipping")
                    else:
                        wait = 2 ** attempt  # 2s, 4s
                        print(f"  Batch {batch_num} attempt {attempt} failed: {exc} — retrying in {wait}s…")
                        time.sleep(wait)

            # Small pause between every batch to avoid overwhelming the free API
            time.sleep(0.1)

            if (i // batch_size) % 100 == 0 and i > 0:
                print(f"  Progress: {i:,} / {len(postcodes):,} postcodes geocoded…")

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
        conn.execute(text("UPDATE voa_list_entries SET has_summary_valuation = FALSE;"))
        conn.execute(
            text("""
                UPDATE voa_list_entries le
                SET has_summary_valuation = TRUE
                WHERE EXISTS (
                    SELECT 1 FROM voa_sv_header sv
                    WHERE sv.uarn = le.uarn
                      AND sv.assessment_reference_number = le.assessment_reference_number
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
            f"Coverage {row.pct}% outside expected 60–95% range — check the assessment-level join"
        )


def create_indexes() -> None:
    """Create indexes after data load for query performance."""
    print("Creating indexes…")
    with engine.connect() as conn:
        for stmt in [
            "CREATE INDEX IF NOT EXISTS idx_le_postcode_sector ON voa_list_entries(postcode_sector)",
            "CREATE INDEX IF NOT EXISTS idx_le_scat_code ON voa_list_entries(scat_code)",
            "CREATE INDEX IF NOT EXISTS idx_le_uarn ON voa_list_entries(uarn)",
            "CREATE INDEX IF NOT EXISTS idx_le_uarn_assessment_ref ON voa_list_entries(uarn, assessment_reference_number)",
            "CREATE INDEX IF NOT EXISTS idx_sv_uarn ON voa_sv_header(uarn)",
            "CREATE INDEX IF NOT EXISTS idx_sv_assessment_ref ON voa_sv_header(assessment_reference_number)",
            "CREATE INDEX IF NOT EXISTS idx_sv_uarn_assessment_ref ON voa_sv_header(uarn, assessment_reference_number)",
            "CREATE INDEX IF NOT EXISTS idx_svl_uarn ON voa_sv_lines(uarn)",
            "CREATE INDEX IF NOT EXISTS idx_svl_assessment_ref ON voa_sv_lines(assessment_reference_number)",
            "CREATE INDEX IF NOT EXISTS idx_svl_uarn_assessment_ref ON voa_sv_lines(uarn, assessment_reference_number)",
            "CREATE INDEX IF NOT EXISTS idx_sva_assessment_ref ON voa_sv_additions(assessment_reference_number)",
            "CREATE INDEX IF NOT EXISTS idx_sva_uarn_assessment_ref ON voa_sv_additions(uarn, assessment_reference_number)",
            "CREATE INDEX IF NOT EXISTS idx_svpm_assessment_ref ON voa_sv_plant_machinery(assessment_reference_number)",
            "CREATE INDEX IF NOT EXISTS idx_svpm_uarn_assessment_ref ON voa_sv_plant_machinery(uarn, assessment_reference_number)",
            "CREATE INDEX IF NOT EXISTS idx_svcp_assessment_ref ON voa_sv_car_parking(assessment_reference_number)",
            "CREATE INDEX IF NOT EXISTS idx_svcp_uarn_assessment_ref ON voa_sv_car_parking(uarn, assessment_reference_number)",
            "CREATE INDEX IF NOT EXISTS idx_svadj_assessment_ref ON voa_sv_adjustments(assessment_reference_number)",
            "CREATE INDEX IF NOT EXISTS idx_svadj_uarn_assessment_ref ON voa_sv_adjustments(uarn, assessment_reference_number)",
            "CREATE INDEX IF NOT EXISTS idx_svat_assessment_ref ON voa_sv_adjustment_totals(assessment_reference_number)",
            "CREATE INDEX IF NOT EXISTS idx_svat_uarn_assessment_ref ON voa_sv_adjustment_totals(uarn, assessment_reference_number)",
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
  sv      <sv_file>               Ingest summary valuations (record types 01–07)
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
