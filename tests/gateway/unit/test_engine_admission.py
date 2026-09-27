from autobuild_json.gateway.admission import InferenceAdmission
from autobuild_json.gateway.engine import Engine
from autobuild_json.gateway.settings import ServiceSettings


def test_service_settings_exposes_inference_capacity():
    assert ServiceSettings(_env_file=None).inference_capacity == 100


def test_direct_engine_uses_capacity_100_admission():
    engine = Engine.__new__(Engine)
    # Constructor wiring is exercised without requiring a database harness.
    Engine.__init__(engine, None, None, None, None, None, None)
    assert isinstance(engine.admission, InferenceAdmission)
    assert engine.admission.capacity == 100
