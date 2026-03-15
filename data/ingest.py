# data/ingest.py
# VOA bulk data ingest — list entries and summary valuations
#
# Usage (from project root):
#   python data/ingest.py <list_entries_file> <summary_valuations_file>
#   Files can be .csv or .zip

import os
import sys
import zipfile

# Allow `from db import ...` regardless of working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from sqlalchemy import text

from db import get_engine

engine = get_engine()

# ── CRITICAL: VOA files are asterisk-delimited ASCII despite .csv extension ──
VOA_DELIMITER = "*"
VOA_ENCODING = "latin-1"  # pragmatic default; spec is ASCII, latin-1 is a superset


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

# SCAT codes for Phase 1 property types (confirmed from VOA spec Appendix 2)
PHASE_1_SCAT_CODES = [249, 251, 409, 234, 417, 85, 416]
# Phase 2 additions: 226, 227 (pubs) — not ingested at Phase 1


def _open_csv(filepath: str, name_fragment: str):
    """Open a CSV from a direct path or from the matching file inside a zip."""
    if filepath.endswith(".zip"):
        with zipfile.ZipFile(filepath) as z:
            candidates = [f for f in z.namelist() if name_fragment in f.lower()]
            if not candidates:
                raise FileNotFoundError(
                    f"No file matching '{name_fragment}' found in {filepath}. "
                    f"Contents: {z.namelist()}"
                )
            return z.open(candidates[0])
    return open(filepath, "r", encoding=VOA_ENCODING)


def ingest_list_entries(filepath: str) -> pd.DataFrame:
    """
    Ingest VOA compiled list entries into voa_list_entries.

    - Filters to Phase 1 SCAT codes.
    - Excludes composite properties (composite_indicator = 'C').
    - Excludes proxy deletion records (rateable_value IS NULL).
    - Excludes launderettes from SCAT 249.
    - Accepts null effective_date and list_alteration_date (see D6c).
    """
    print(f"Reading list entries from {filepath}...")

    with _open_csv(filepath, "listentries") as f:
        df = pd.read_csv(
            f,
            sep=VOA_DELIMITER,
            encoding=VOA_ENCODING,
            header=None,
            names=LIST_ENTRY_COLUMNS,
            dtype=str,
            low_memory=False,
        )

    print(f"Raw row count: {len(df):,}")

    # Validate delimiter — if row count is tiny, delimiter is wrong
    assert len(df) > 100_000, (
        f"Row count {len(df)} is suspiciously low. "
        "Check the delimiter is '*' not ','."
    )

    # Extract numeric SCAT code (strip suffix letter, e.g. "249S" → 249)
    df["scat_code"] = pd.to_numeric(
        df["scat_code_and_suffix"].str.extract(r"^(\d+)")[0],
        errors="coerce",
    )

    # Filter to Phase 1 SCAT codes
    df = df[df["scat_code"].isin(PHASE_1_SCAT_CODES)]
    print(f"After SCAT filter: {len(df):,} rows")

    # Exclude composite properties
    df = df[df["composite_indicator"].str.strip() != "C"]

    # Exclude proxy deletion records (null rateable_value)
    df["rateable_value"] = pd.to_numeric(df["rateable_value"], errors="coerce")
    df = df[df["rateable_value"].notna()]

    # Type conversions
    df["uarn"] = pd.to_numeric(df["uarn"], errors="coerce")
    df["assessment_reference_number"] = pd.to_numeric(
        df["assessment_reference_number"], errors="coerce"
    )
    df["postcode"] = df["postcode"].str.strip().str.upper()

    # Derive postcode sector for geographic pre-filtering (e.g. "SW19 1AB" → "SW19 1")
    df["postcode_sector"] = df["postcode"].str.extract(
        r"^([A-Z]{1,2}\d{1,2}[A-Z]?\s\d)"
    )[0]

    # Modelling rule (D4): exclude launderettes from SCAT 249
    launderette_mask = (df["scat_code"] == 249) & (
        df["primary_description_text"].str.contains("LAUNDERETTE", case=False, na=False)
    )
    df = df[~launderette_mask]
    print(f"After launderette exclusion: {len(df):,} rows")

    cols_to_store = [
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
        "effective_date",       # kept as string; NULL is valid (see D6c)
        "list_alteration_date", # kept as string; NULL is valid (see D6c)
        "composite_indicator",
        "current_from_date",
        "current_to_date",
    ]
    df = df[cols_to_store]

    print("Writing to voa_list_entries...")
    df.to_sql(
        "voa_list_entries",
        engine,
        if_exists="replace",
        index=False,
        chunksize=5_000,
        method="multi",
    )
    print(f"Done. {len(df):,} rows written to voa_list_entries.")
    return df


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


def parse_summary_valuations(filepath: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Parse the multi-record-type summary valuation file.
    Returns (df_headers, df_lines) for record types 01 and 02.

    Record types 03–07 are not used in Phase 1 and are skipped.
    """
    print(f"Parsing summary valuations from {filepath}...")

    headers = []
    lines = []
    current_uarn = None
    current_assessment_ref = None

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
            headers.append(row)

        elif record_type == "02":
            padded = fields + [""] * (len(SV_LINE_COLUMNS) - len(fields))
            row = dict(zip(SV_LINE_COLUMNS, padded[: len(SV_LINE_COLUMNS)]))
            row["uarn"] = current_uarn
            row["assessment_reference_number"] = current_assessment_ref
            lines.append(row)

        # Record types 03–07: not needed for Phase 1 — skip

    raw_file.close()

    df_headers = pd.DataFrame(headers)
    df_lines = pd.DataFrame(lines)
    print(f"Parsed {len(df_headers):,} summary valuation headers")
    print(f"Parsed {len(df_lines):,} line items")
    return df_headers, df_lines


def ingest_summary_valuations(filepath: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Ingest summary valuation headers and line items.

    Stores all unit_of_measurement values; NIA filtering is applied at
    query time by the comparables engine, not at ingest time.
    """
    df_headers, df_lines = parse_summary_valuations(filepath)

    # Type conversions for headers
    df_headers["uarn"] = pd.to_numeric(df_headers["uarn"], errors="coerce")
    df_headers["assessment_reference_number"] = pd.to_numeric(
        df_headers["assessment_reference_number"], errors="coerce"
    )
    df_headers["unadjusted_price_psm"] = pd.to_numeric(
        df_headers["unadjusted_price_psm"], errors="coerce"
    )
    # D6b: total_area_or_units is area for NIA, unit count otherwise — coerce numeric
    df_headers["total_area_or_units"] = pd.to_numeric(
        df_headers["total_area_or_units"], errors="coerce"
    )
    df_headers["adopted_rv"] = pd.to_numeric(df_headers["adopted_rv"], errors="coerce")

    # Flag NIA properties (the only valid zoning-method comparables — see D6a)
    df_headers["is_nia"] = df_headers["unit_of_measurement"].str.strip() == "NIA"

    print("Writing to voa_sv_header...")
    df_headers.to_sql(
        "voa_sv_header",
        engine,
        if_exists="replace",
        index=False,
        chunksize=5_000,
        method="multi",
    )

    # Line items
    df_lines["uarn"] = pd.to_numeric(df_lines["uarn"], errors="coerce")
    df_lines["area"] = pd.to_numeric(df_lines["area"], errors="coerce")
    df_lines["price"] = pd.to_numeric(df_lines["price"], errors="coerce")
    df_lines["value"] = pd.to_numeric(df_lines["value"], errors="coerce")

    print("Writing to voa_sv_lines...")
    df_lines.to_sql(
        "voa_sv_lines",
        engine,
        if_exists="replace",
        index=False,
        chunksize=5_000,
        method="multi",
    )

    print(
        f"Done. {len(df_headers):,} headers and {len(df_lines):,} lines written."
    )
    return df_headers, df_lines


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

    print(f"Geocoding {len(postcodes):,} unique postcodes via postcodes.io...")

    coords = []
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
    df_coords.to_sql(
        "postcode_coords",
        engine,
        if_exists="replace",
        index=False,
        chunksize=5_000,
        method="multi",
    )

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
    print("Adding has_summary_valuation flag...")
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
    print("Creating indexes...")
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

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python data/ingest.py <list_entries_file> <summary_valuations_file>")
        print("Files can be .csv or .zip")
        sys.exit(1)

    list_file = sys.argv[1]
    sv_file = sys.argv[2]

    ingest_list_entries(list_file)
    ingest_summary_valuations(sv_file)
    add_summary_valuation_flag()
    geocode_postcodes()
    create_indexes()

    print("\nIngest complete. Run queries against voa_list_entries and voa_sv_header.")
