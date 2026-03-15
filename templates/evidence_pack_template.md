# Business Rates Evidence Pack

**Prepared for:** {{business_name}}
**Property Address:** {{property_address}}
**UPRN:** {{uprn}}
**Postcode:** {{postcode}}
**Sector:** {{business_type}}
**Date Prepared:** {{date_prepared}}
**Prepared By:** {{prepared_by}}

---

## 1. Executive Summary

- **VOA RV (2026 list):** £{{voa_rv}}
- **Modelled RV (this analysis):** £{{modelled_rv}}
- **Difference (overvaluation + / undervaluation -):** £{{rv_delta}} ({{rv_delta_pct}}%)
- **Estimated annual saving (if RV reduced):** £{{annual_saving_estimate}}
- **Case Strength:** {{case_strength}} (High / Medium / Low)

> **Context:** The 2026 list uses an AVD of 1 April 2024. This pack applies the VOA's
> published valuation methodologies for the relevant property type to derive a fair benchmark
> and present structured evidence for Check/Challenge.

---

## 2. Basis of Valuation (Methodology)

**2.1 Method used here**

- **Restaurants/Cafés/Retail/Hair/Beauty:** Rental comparison using Net Internal Area (NIA)
  and **zoning** (Zone A/B/C/remainder). Zones reflect decreasing value with depth. Area types
  (e.g., visible kitchen, stores) are weighted accordingly.
- **Nurseries:** Rental comparison using **NIA × £/m²** (no zoning), with adjustments for
  design, use and location.

**2.2 Evidence window and AVD**

- Comparable rents are time-adjusted to the **AVD: 1 April 2024**.
- Tone (£/m² Zone A or NIA) is derived via weighted median after outlier filtering.

---

## 3. Property Facts

- **VOA Description:** {{voa_description}}
- **NIA (sqm):** {{nia_sqm}}
- **Frontage (m):** {{frontage_m}}
- **Depth (m):** {{depth_m}}
- **Floors:** {{floors}}
- **Layout Notes:** {{layout_notes}}

### 3.1 Area Breakdown

- **Dining / Sales area (sqm):** {{sales_area_sqm}}
- **Visible kitchen (sqm):** {{visible_kitchen_sqm}}
- **Non-visible kitchen (sqm):** {{non_visible_kitchen_sqm}}
- **Storage / BOH (sqm):** {{storage_sqm}}
- **Basement storage (sqm):** {{basement_sqm}}
- **Upper floor / mezzanine (sqm):** {{upper_sqm}}
- **Outdoor seating (if any):** {{outdoor_seating_details}}

*(Nurseries: list internal areas and key design factors; no zoning.)*

---

## 4. Comparable Rental Evidence (CSA)

**4.1 Selection & Normalisation**

- Same or equivalent use, proximate location, similar size.
- Time-adjusted to AVD (2024-04-01).
- Normalised to **Zone-A equivalents** (retail/restaurants) or **NIA £/m²** (nurseries).
- Outliers removed (top/bottom 10%); suspicious evidence down-weighted.
- Distance weightings: same parade (1.0), close (0.7), broader (0.5), fallback (0.4).

**4.2 Comparable Table**

| ID | Address | Use | Lease date | Rent p.a. | NIA (sqm) | ZA-eq (sqm)* | £/m² (ZA or NIA) | Distance (m) | Weight | Rate source | Notes |
|----|---------|-----|------------|-----------|-----------|--------------|------------------|--------------|--------|-------------|-------|
{{#each comparables}}
| {{id}} | {{address}} | {{use}} | {{lease_date}} | £{{rent_pa}} | {{nia_sqm}} | {{za_equiv_sqm}} | £{{tone_psm}} | {{distance_m}} | {{weight}} | {{rate_source}} | {{notes}} |
{{/each}}

\* ZA-eq applies to zoned valuations (retail/restaurants).
\*\* Rate source: voa_published = VOA unadjusted primary survey unit rate (field 29, summary valuation);
implied = back-calculated from total area using 6.1m standard zone depth.

**4.3 Tone Derivation**

- **Final tone:** £{{final_tone_psm}} per m² ({{tone_basis}})
- **Confidence:** {{confidence}} (High/Medium/Low)

---

## 5. Valuation Calculation

### 5.1 Zoning Schedule (if applicable)

| Zone | Area (sqm) | Relativity | Area Type Weight | Tone (£/m²) | Value (£) |
|------|------------|------------|-----------------|-------------|-----------|
{{#each zoning_rows}}
| {{zone}} | {{area_sqm}} | {{relativity}} | {{weight}} | £{{tone_psm}} | £{{value}} |
{{/each}}

**Subtotal (pre-allowances):** £{{subtotal_pre}}
**Allowances (with rationale):** {{allowances_summary}}
**Modeled RV (rounded):** **£{{modelled_rv}}**

### 5.2 Nursery Calculation (if applicable)

**NIA × Tone:** {{nia_sqm}} × £{{final_tone_psm}}/m² = £{{subtotal_pre}}
**Adjustments (design/use/location):** {{nursery_adjustments}}
**Modeled RV (rounded):** **£{{modelled_rv}}**

---

## 6. Conclusion & Recommendation

- **VOA RV:** £{{voa_rv}}
- **Modeled RV:** £{{modelled_rv}}
- **Difference:** £{{rv_delta}} ({{rv_delta_pct}}%)
- **Case Strength:** {{case_strength}}

**Recommendation:**
{{recommendation_text}}
(e.g., "Proceed to Check with this evidence. If not accepted, proceed to Challenge.")

---

## 7. Submission-Ready Narrative (Copy/Paste)

> **Subject:** Correction Request – {{property_address}}
>
> We request correction of the 2026 Rateable Value, applying the VOA's valuation method for
> {{business_type}}. We derived a local tone (£/m²) from comparable evidence normalised to the
> AVD (1 April 2024), and applied zoning (where relevant) and area type weightings consistent
> with VOA practice. Our valuation schedule (zones/NIA), adjustments, and comparable tables
> are attached.
>
> **VOA RV:** £{{voa_rv}}; **Modeled RV:** £{{modelled_rv}}.
>
> We ask that the VOA updates the assessment accordingly.

---

## 8. Appendices

- **Appendix A – Comparable Evidence (full)**
  Detailed rows, normalisation steps, rate source flags, maps.
- **Appendix B – Zoning/NIA Schedules**
  Zone breakdowns or NIA calc printouts.
- **Appendix C – Floor Plans & Photos**
  Frontage, interior, kitchen visibility, storage.
- **Appendix D – Methodology References**
  Links/extracts to VOA method guidance (restaurants retail-method, nurseries NIA,
  pubs FMT, AVD 2024-04-01, summary valuation data specification).
