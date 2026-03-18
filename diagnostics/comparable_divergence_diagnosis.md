# Diagnosis: quick_test.py vs Web App Comparable Divergence

**Cases:** SW20 8TR / Nursery / 120 sqm and SW20 8TR / Restaurant / 80 sqm

---

## 1. Concise Diagnosis

The two paths diverge at the **`get_comparables()` call**, not in the CSA. For **Nursery**, `assess.py` passes `radius_m=2000` (the generic fallback from `csa.yaml`) while `quick_test.py` explicitly passes `radius_m=10_000`; nurseries are sparse enough in SW20 that the 2km bounding box returns zero candidates before the CSA even runs. For **Restaurant**, the DB retrieval is broadly equivalent, but the web form defaults `voa_rv=0` (`models.py` line 28), which causes the mandatory restaurant quality gate in `run_csa()` (csa.py lines 1118–1122) to immediately reject with `"missing_subject_implied_rate"` — a path `quick_test.py` never hits because it always has real VOA rateable values from the database.

---

## 2. Side-by-Side Trace Table

### Case 1 — SW20 8TR / Nursery / 120 sqm

| Stage | quick_test.py | Web app (assess.py) | First divergence? |
|---|---|---|---|
| **Postcode → coords** | `postcode_coords` table lookup (`get_coords_batch`) | `postcodes.io` HTTP API (`postcode_to_coords`) | No — same coordinates for a valid postcode |
| **Segment mapping** | `scat_codes=[85]`, `business_type="nursery"` | `scat_codes=[sc["nursery"]]=[85]`, `btype="nursery"` | No — identical |
| **`get_comparables()` radius** | `radius_m=10_000` (explicit, lines 210–211) | `radius_m=rules["filters"]["distance_m"]["fallback"]=2000` (assess.py line 46) | **YES — first divergence** |
| **`get_comparables()` size_band** | `size_band_pct=50` | `size_band_pct=rules["filters"]["size_band_pct_fallback"]=50` | No — same |
| **DB ±30% RV/sqm outlier removal** | Applied inside `get_comparables()` | Applied inside `get_comparables()` | No — same function |
| **Raw candidates returned** | Nurseries within 10km — likely several | Nurseries within 2km — likely **zero or < 2** | Flows from radius divergence |
| **CSA distance ceiling** | `max_radius=_NURSERY_RADIUS_M=10_000` (csa.py line 728) | Same constant used, but pool fed in was already capped at 2km | Moot — pool is empty |
| **CSA min-comps check** | `_NURSERY_MIN_COMPS=2` — passes | `_NURSERY_MIN_COMPS=2` — fails (0 candidates) | Returns `_insufficient_data()` |
| **Result** | Comparables found; estimated RV produced | `"Insufficient Data"` | — |

**Note:** `_NURSERY_RADIUS_M = 10_000` is defined in `csa.py` (line 231) and correctly used as the distance-filter ceiling inside `run_csa()`. But that only matters if `get_comparables()` was called with a matching radius. The web app never passes the nursery-specific radius to the DB query.

---

### Case 2 — SW20 8TR / Restaurant / 80 sqm

| Stage | quick_test.py | Web app (assess.py) | First divergence? |
|---|---|---|---|
| **Postcode → coords** | `postcode_coords` table | `postcodes.io` API | No |
| **Segment mapping** | `scat_codes=[409, 234]`, `business_type="restaurant_cafe"` | `scat_codes=[sc["cafe"], sc["restaurant"]]=[409, 234]` | No — identical |
| **`get_comparables()` radius** | `radius_m=1_500` (lines 213–214) | `radius_m=3000` (assess.py line 46) | No — web app is wider; more candidates |
| **`get_comparables()` size_band** | `size_band_pct=50` | `size_band_pct=75` | No — web app is more permissive |
| **Raw candidates returned** | Restaurants within 1.5km | Restaurants within 3km | Web app has equal or more candidates |
| **CSA size re-filter** | `size_pct=_RESTAURANT_SIZE_BAND_PCT=75` | Same | No |
| **CSA hard distance guard** | `d <= _RESTAURANT_MAX_DISTANCE_M=1500` | Same constant applied | No |
| **CSA median distance guard** | `_median_distance_m <= 1000` or reject | Same | No |
| **`voa_rv` value passed to CSA** | `row["rateable_value"]` — real DB value (always > 0) | `req.property.voa_rv` — defaults to **0** (`models.py` line 28) | **YES — first divergence (when user omits RV)** |
| **Restaurant quality gate** | `_restaurant_implied_rate = voa_rv / _s_itza` → valid float; gate proceeds | `voa_rv=0` → `(voa_rv and voa_rv > 0)` = False → `_restaurant_implied_rate=None` → `rejection_reason="missing_subject_implied_rate"` (csa.py 1120–1122) | Immediate rejection |
| **Result** | Comparables found; rate-distance check proceeds | `"Insufficient Data"` | — |

**Note:** Even if the user supplies a valid `voa_rv`, the rate-distance gate at csa.py lines 1124–1169 can still reject if `|tone - implied_rate| > £50/m²` (`rate_distance_gt_50`) or if the pool has only 2 comps with rate_distance > 35. `quick_test.py` properties are sampled from real VOA records whose actual RV is consistent with local tone, so they tend to pass the gate. A user who is genuinely overassessed (their VOA RV is higher than the local tone) can be rejected precisely by the over-stringent quality gate — the opposite of the desired behaviour.

---

## 3. Exact Root Cause

### Nursery — different radius constant, not wired into the web app

The constant `_NURSERY_RADIUS_M = 10_000` (csa.py line 231) is used inside `run_csa()` as the CSA-internal distance ceiling (line 728). `quick_test.py` also independently passes `radius_m=10_000` to `get_comparables()` (lines 209–211). **`assess.py` does neither.** It falls into the generic `else` branch:

```python
# assess.py line 46
_radius_m = 3000 if btype == "restaurant_cafe" else rules["filters"]["distance_m"]["fallback"]
#                                                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#                                                   = 2000 for nursery — wrong
```

The DB bounding box is 2km. No nursery-specific branch exists. The CSA's own 10km ceiling is irrelevant when the input pool is empty.

### Restaurant — `voa_rv=0` default defeats mandatory quality gate

```python
# models.py line 28
voa_rv: float = 0          # ← default; user may not fill this in
```

```python
# csa.py lines 1117–1122
_s_itza = itza_from_nia(nia_sqm, zone_depth)
_restaurant_implied_rate = (voa_rv / _s_itza) if (voa_rv and voa_rv > 0 and _s_itza > 0) else None
if _restaurant_implied_rate is None:
    restaurant_rejection_reason = "missing_subject_implied_rate"
    restaurant_quality_gate_passed = False  # → returns _insufficient_data()
```

`quick_test.py` never hits this because it reads `rateable_value` from the VOA DB, which is always > 0. The web form does not require this field, so `0` silently propagates.

---

## 4. Minimum Fix Areas

*Identification only — no implementation.*

| Case | File | Line(s) | What needs to change |
|---|---|---|---|
| **Nursery radius** | `api/routes/assess.py` | 46 | Add a nursery-specific branch: `elif btype == "nursery": _radius_m = 10_000` (matching `_NURSERY_RADIUS_M` in csa.py) |
| **Restaurant `voa_rv=0`** | `api/routes/assess.py` + `api/engine/csa.py` | assess.py 88–95; csa.py 1118–1122 | Either (a) make `voa_rv` required / validated > 0 before calling `run_csa()`, or (b) allow the restaurant quality gate to fall back to a lower confidence band when `_restaurant_implied_rate is None` rather than hard-rejecting |
| **Restaurant over-stringent rejection** | `api/engine/csa.py` | 1151–1169 | The `rate_distance_gt_50` hard rejection and `downward_biased_cluster` rejection fire on genuine overassessment cases — separate correctness issue, not the primary cause of "no result" for the test inputs |

---

## 5. Are quick_test.py and the Web App Running the Same Code?

Yes — they import and call the same `get_comparables()`, `run_csa()`, and `csa_rules()` functions from the same modules. The divergence is entirely in the **calling parameters**, not in any stale or alternate code version:

| Parameter | quick_test.py | Web app |
|---|---|---|
| Nursery DB radius | `10_000` | `2_000` (fallback) |
| Restaurant DB radius | `1_500` | `3_000` (explicit override) |
| Restaurant size_band | `50` | `75` |
| `voa_rv` source | VOA DB — always > 0 | Web form — defaults to `0` |
| `subject_sv_line_descs` | Fetched from `voa_sv_lines` table | `()` empty (not available from form) |
| `subject_description` | VOA `primary_description_text` | `""` empty |
| `subject_postcode_sector` | Sector string for retail; `""` for nursery/restaurant | `""` always (not passed) |

The `subject_sv_line_descs` / `subject_description` omission in the web app does **not** affect either test case: nurseries don't use ITZA classification, and restaurants default correctly to `itza_retail` for rate extraction.
