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
| 18      | rateable_value               | The RV                                         |
| 16      | effective_date               | When assessment came into force                |
| 17      | composite_indicator          | C = mixed domestic/non-domestic                |

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
| 17      | total_area                   | Total area in m²                               |
| 18      | sub_total                    | Pre-adjustment valuation total                 |
| 20      | adopted_rv                   | Final RV as shown in rating list               |
| 27      | scat_code                    | Property type                                  |
| 28      | unit_of_measurement          | NIA or GIA — critical for comparability        |
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

Condition: has_summary_valuation = false OR unit_of_measurement = 'GIA'

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

GIA filter: for zoning-method comparables, only use rows where
unit_of_measurement = 'NIA'. GIA properties are not directly comparable
on a £/m² Zone A basis.

---

## D5: Update Cadence

- Compiled 2026 list goes live 1 April 2026 — download on that date
- Epochs refreshed periodically; bi-monthly observed for 2017 list but not
  guaranteed as a 2026 SLA — treat as approximate
- Weekly change-update files accumulate between epochs
- Recommended schedule: full re-ingest on each new epoch; weekly change-update
  ingest in between
- Monitor for new files via Azure Blob API:
  https://voaratinglists.blob.core.windows.net/downloads?restype=container&comp=list
