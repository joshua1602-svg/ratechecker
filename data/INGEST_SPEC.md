# RateCheck — VOA Data Ingest Specification

## D1: File Format — Critical

VOA bulk files use asterisk as the field delimiter despite having a `.csv` extension.
The official spec states: "variable length fields delimited by an asterisk."

All pandas ingest must use:

    pd.read_csv(filepath, sep='*', encoding='latin-1')
    # encoding note: spec states ASCII; latin-1 is a pragmatic default — it is a
    # strict superset of ASCII so reads all ASCII content correctly and handles
    # unexpected non-ASCII bytes without raising an error. Not a spec requirement.

Do NOT use default comma separator. It will silently produce garbage rows with no
error raised — the single most dangerous failure mode in this pipeline.

---

## D2: Two Files, Not One

The VOA publishes list entries and summary valuations as separate files. Both must
be ingested. Ingest list entries first, then summary valuations, then join.

### File 1 — List Entries → table: voa_list_entries

One row per property assessment. Columns to ingest:

| Field # | Column name                  | Notes                                          |
|---------|------------------------------|------------------------------------------------|
| 7       | uarn                         | Primary hereditament key — use this not BA ref |
| 20      | assessment_reference_number  | Keep — needed for multi-assessment history     |
| 2       | ba_code                      | Billing authority                              |
| 22      | scat_code                    | Property type — primary filter                 |
| 5       | primary_description_code     | e.g. CS = shop                                 |
| 6       | primary_description_text     | Human-readable description                     |
| 15      | postcode                     | Geographic filter                              |
| 8       | full_property_identifier     | Address string for display                     |
| 18      | rateable_value               | The RV; null for proxy deletion records — exclude these |
| 16      | effective_date               | When assessment came into force; **null for revaluation assessments** (treat as 2026-04-01 in DB) |
| 17      | composite_indicator          | C = mixed domestic/non-domestic                |
| 21      | list_alteration_date         | Date of list change; **null for revaluation assessments** — must not reject null here |

Exclusion rule: drop any row where composite_indicator = 'C'.
Mixed-use properties have apportioned RVs and are invalid as comparables.

Derived column: after joining with summary valuations, add boolean
has_summary_valuation — true where a matching record exists in voa_sv_header.

### File 2 — Summary Valuations → two tables

The summary valuation file contains 7 record types per property in a single file,
all linked by both uarn and assessment_reference_number. Parse into:

Table: voa_sv_header (record type 01)

| Field # | Column name                  | Notes                                          |
|---------|------------------------------|------------------------------------------------|
| 3       | uarn                         | Join key to list entries                       |
| 2       | assessment_reference_number  | Secondary join key — preserve for history      |
| 17      | total_area_or_units          | Area in m² for area-based properties; unit count for unit-based properties — **ambiguous field**, coerce to numeric and treat as area only for NIA records (see D6) |
| 18      | sub_total                    | Pre-adjustment valuation total                 |
| 20      | adopted_rv                   | Final RV as shown in rating list               |
| 27      | scat_code                    | Property type                                  |
| 28      | unit_of_measurement          | **NIA, GIA, EFA, GEA, RCA, OTH** — six possible values; zoning-method comparables must be NIA only, exclude all others (see D3, D6) |
| 29      | unadjusted_price_psm         | VOA unadjusted primary survey unit rate (£/m²) |

Note on field 29: The spec calls this "Unadjusted Matrix price (£/m²) for the
primary survey unit." For retail/food/beverage properties valued on the zoning
method, this corresponds to the Zone A rate. For other valuation schemes
(contractor's basis, receipts and expenditure, etc.) it represents a different
unit rate concept. Column is named unadjusted_price_psm in the schema — do NOT
rename it zone_a_rate at schema level. Apply the Zone A interpretation in
application logic only, scoped to zoning-method SCAT codes.

Table: voa_sv_lines (record type 02)

| Field    | Column name                  | Notes                                          |
|----------|------------------------------|------------------------------------------------|
| parent   | uarn                         | Inherited from parent record type 01           |
| parent   | assessment_reference_number  | Inherited from parent record type 01           |
| 2        | line_number                  | Order of line items                            |
| 3        | floor                        | e.g. ground, first, mezzanine                  |
| 4        | description                  | e.g. Zone A, Zone B, ancillary storage         |
| 5        | area                         | Area of the line item (m²)                     |
| 6        | price                        | Price applied (£/m²)                           |
| 7        | value                        | Value of the line item (£)                     |

Join: voa_list_entries LEFT JOIN voa_sv_header ON uarn. Left join preserves all
list entries including the ~20% without summary valuation data.

---

## D3: Comparables Engine — Rate Derivation

Two-tier approach. The tier used for each comparable must be recorded as
rate_source and surfaced in all report outputs.

### Tier 1 — VOA-published rate (preferred)

Condition: has_summary_valuation = true AND unit_of_measurement = 'NIA'
AND scat_code in retail/zoning-method set

Rate: voa_sv_header.unadjusted_price_psm
Label: rate_source = 'voa_published'

This is the VOA's own unadjusted primary survey unit rate. For retail and
food/beverage properties valued on the zoning method, treat as the Zone A
equivalent rate. This materially reduces (but does not eliminate) comparability
uncertainty — property type fit, locality, and survey basis must still be
assessed per standard CSA rules.

### Tier 2 — Implied rate (fallback)

Condition: has_summary_valuation = false OR unit_of_measurement != 'NIA'

Rate: back-calculate using standard 6.1m zone depth assumption:
      implied_zone_a_rate = rateable_value / itza_from_total_area
Label: rate_source = 'implied'

Approximately 20% of properties fall into this tier. The 80% figure for
survey-supported valuations is approximate and varies by property class.

---

## D4: SCAT Codes

Confirmed from VOA 2026 Data Specification Appendix 2:

| SCAT | Description               | Phase   |
|------|---------------------------|---------|
| 249  | Shops                     | Phase 1 |
| 251  | Showrooms                 | Phase 1 |
| 409  | Cafés                     | Phase 1 |
| 234  | Restaurants               | Phase 1 |
| 417  | Hairdressing/Beauty       | Phase 1 |
| 85   | Day Nurseries/Play Schools| Phase 1 |
| 416  | Gymnasia/Fitness Suites   | Phase 1 |
| 226  | Public Houses             | Phase 2 |
| 227  | Public Houses (with lodge)| Phase 2 |

Launderette exclusion (modelling rule — not in VOA spec):
SCAT 249 includes launderettes. Exclude with:
  drop rows where scat_code=249 AND primary_description_text ILIKE '%LAUNDERETTE%'

Measurement filter: for zoning-method comparables, only use rows where
unit_of_measurement = 'NIA'. All other measurement types (GIA, EFA, GEA,
RCA, OTH) must be excluded — they are not directly comparable on a
£/m² Zone A basis.

---

## D5: Update Cadence

- Compiled 2026 list goes live 1 April 2026 — download on that date
- Bi-monthly epoch cadence **confirmed in writing** in the compiled list specification: "We look to do this bi-monthly." Treat as the expected schedule, not just an observation.
- Weekly change-update files accumulate between epochs
- Recommended schedule: full re-ingest on each new epoch; weekly change-update
  ingest in between
- Monitor for new files via Azure Blob API:
  https://voaratinglists.blob.core.windows.net/downloads?restype=container&comp=list

---

## D6: Compiled List Specification — Corrections to Earlier Notes

These corrections come from the 2026 compiled list documentation and supersede
any conflicting statements elsewhere in this spec.

### D6a: Field 28 (unit_of_measurement) — six values, not two

The compiled list spec defines six valid values for field 28:

| Value | Meaning                  | Use in comparables engine                      |
|-------|--------------------------|------------------------------------------------|
| NIA   | Net Internal Area        | **Include** — only valid basis for zoning ITZA |
| GIA   | Gross Internal Area      | Exclude from zoning-method comparables         |
| EFA   | Effective Floor Area     | Exclude                                        |
| GEA   | Gross External Area      | Exclude                                        |
| RCA   | Reduced Covered Area     | Exclude                                        |
| OTH   | Other                    | Exclude                                        |

Filter logic: `WHERE unit_of_measurement = 'NIA'` (not `!= 'GIA'`).
Any value other than NIA is invalid as a zoning-method comparable.

### D6b: Field 17 (total_area_or_units) — ambiguous for unit-based properties

The compiled list spec labels this field "Total Area / Total Units."
For area-based properties (NIA, GIA, etc.) it is m². For unit-based properties
(e.g. car parks charged per space) it is a unit count.

Ingest rule:
- Coerce to numeric; non-numeric or negative values → NULL.
- Treat as area (m²) only when unit_of_measurement = 'NIA'.
- Do not use this field for non-NIA properties in the comparables engine.
- Column name in schema: `total_area_or_units` (not `total_area`).

### D6c: Null dates for revaluation assessments

`effective_date` (field 16, list entries) and `list_alteration_date`
(field 21, list entries) are **output as null** in the compiled list file for
properties on the list from the revaluation date (1 April 2026).

These properties are valid and should **not** be rejected. Ingest as NULL;
application logic should treat NULL effective_date as 2026-04-01.
