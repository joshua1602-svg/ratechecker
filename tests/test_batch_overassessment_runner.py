from scripts.batch_overassessment_runner import _banding, _confidence_score, _infer_business_type


def test_banding_thresholds():
    assert _banding(voa_rv=120000, fair_high=90000, pct_gap=25) == ("strong", True)
    assert _banding(voa_rv=105000, fair_high=95000, pct_gap=12) == ("moderate", True)
    assert _banding(voa_rv=101000, fair_high=100000, pct_gap=2) == ("borderline", True)
    assert _banding(voa_rv=90000, fair_high=100000, pct_gap=-5) == ("not_flagged", False)


def test_confidence_score_respects_signal_and_comp_count():
    assert _confidence_score("High", 10) == 0.95
    assert _confidence_score("Low", 1) == 0.35


def test_infer_business_type_from_scat_fallback():
    assert _infer_business_type(None, 249) == "retail"
