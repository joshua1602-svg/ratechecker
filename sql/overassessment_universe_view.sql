-- Optional helper view for the batch runner.
-- Provides a canonical input shape expected by scripts/batch_overassessment_runner.py.

CREATE OR REPLACE VIEW overassessment_universe AS
SELECT
    le.uarn::text AS id,
    le.uarn::text AS uarn,
    le.full_property_identifier AS address,
    le.postcode,
    CASE
        WHEN le.scat_code IN (409) THEN 'restaurant_cafe'
        WHEN le.scat_code IN (249, 251) THEN 'retail'
        WHEN le.scat_code IN (203) THEN 'hair_beauty'
        WHEN le.scat_code IN (85) THEN 'nursery'
        WHEN le.scat_code IN (218, 806) THEN 'pub'
        ELSE NULL
    END AS business_type,
    le.scat_code,
    le.rateable_value::numeric AS voa_rv,
    svh.total_area_or_units::numeric AS nia_sqm
FROM voa_list_entries le
LEFT JOIN voa_sv_header svh
    ON svh.uarn = le.uarn
WHERE le.rateable_value > 0
  AND svh.unit_of_measurement = 'NIA'
  AND svh.total_area_or_units > 0;
