import threading
import time
import pytest
from autobuild_json.models import AttemptContext, RunResult
from autobuild_json.errors import FlowError

def test_cancel_deadline_and_success_invariants():
    cancel = threading.Event()
    context = AttemptContext(time.monotonic() + 100, cancel, lambda *args: None)
    assert context.remaining_timeout() <= 30
    cancel.set()
    with pytest.raises(FlowError) as error:
        context.check()
    assert error.value.code == "CANCELLED"
    with pytest.raises(ValueError):
        RunResult("success")
    context = AttemptContext(0, threading.Event(), lambda *args: None)
    with pytest.raises(FlowError) as error:
        context.check()
    assert error.value.code == "TIMEOUT"
