"""The connector-specs log line names an unset cap (#588)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.transcription.base import ConnectorSpecifications
from src.tasks.processing import _connector_specs_log


def test_none_duration_logs_unlimited():
    line = _connector_specs_log(ConnectorSpecifications(max_duration_seconds=None,
                                                        handles_chunking_internally=True))
    assert line == "Connector specs: max_duration=unlimited, handles_internally=True, recommended_chunk=600s"


def test_zero_chunk_stays_zero():
    line = _connector_specs_log(ConnectorSpecifications(max_duration_seconds=None, recommended_chunk_seconds=0))
    assert "recommended_chunk=0s" in line


def test_numeric_duration_keeps_suffix():
    line = _connector_specs_log(ConnectorSpecifications(max_duration_seconds=7200,
                                                        handles_chunking_internally=False,
                                                        recommended_chunk_seconds=5760))
    assert line == "Connector specs: max_duration=7200s, handles_internally=False, recommended_chunk=5760s"
