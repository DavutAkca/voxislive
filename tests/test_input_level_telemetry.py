"""Raw input metering must survive faults in downstream speech processing."""

from types import SimpleNamespace

import numpy as np
import pytest

from app.pipeline import IncomingPipeline


def test_input_level_is_observed_before_speech_consumer_failure():
    pipe = IncomingPipeline.__new__(IncomingPipeline)
    pipe.input_level = 0.0
    pipe._recorder = None
    pipe._source = SimpleNamespace(
        feed=lambda _chunk: (_ for _ in ()).throw(RuntimeError("VAD failed")))

    with pytest.raises(RuntimeError, match="VAD failed"):
        pipe._ingest_input(np.full(512, 0.1, dtype=np.float32))

    assert pipe.input_level > 0.0
