import types

from scripts.batch_overassessment_runner import (
    _banding,
    _confidence_score,
    _infer_business_type,
    _resolve_live_assess_callable,
)


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


def test_resolver_falls_back_to_route_callable(monkeypatch):
    fake_route_module = types.SimpleNamespace(run_assessment_pipeline=lambda req: req)

    def _fake_import(name):
        if name == "api.services.assessment":
            raise ModuleNotFoundError(name)
        if name == "api.routes.assess":
            return fake_route_module
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("scripts.batch_overassessment_runner.importlib.import_module", _fake_import)
    fn = _resolve_live_assess_callable()
    assert fn is fake_route_module.run_assessment_pipeline
