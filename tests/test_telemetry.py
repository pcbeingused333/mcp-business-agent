"""
Tests for the metric emitter.

The thing being pinned here is the *shape* of an EMF record, because CloudWatch
is unforgiving about it in the worst possible way: a malformed `_aws` block is
not an error, it is a log line. The metrics simply never appear, the dashboard
stays empty, and nothing anywhere says why. That is the same failure mode as an
eval that scores nothing and exits zero, so it gets the same treatment.
"""
import io
import json

import pytest

from ops import telemetry


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(telemetry, "ENABLED", True)
    telemetry.reset_cold_start_for_tests()


def _emit_to_string(**kwargs) -> dict:
    buffer = io.StringIO()
    telemetry.emit(stream=buffer, **kwargs)
    return json.loads(buffer.getvalue())


def test_a_record_carries_the_aws_block_cloudwatch_extracts_metrics_from():
    record = _emit_to_string(
        metrics={"RequestLatencyMs": 12.5},
        units={"RequestLatencyMs": "Milliseconds"},
        dimensions={"Kind": "mcp"},
    )

    block = record["_aws"]["CloudWatchMetrics"][0]
    assert block["Namespace"] == telemetry.NAMESPACE
    assert block["Metrics"] == [{"Name": "RequestLatencyMs", "Unit": "Milliseconds"}]
    # The dimension name is declared in the block AND its value sits at the top
    # level of the record. Declaring one without the other is the most common way
    # to get a silently empty metric.
    assert block["Dimensions"] == [["Kind"]]
    assert record["Kind"] == "mcp"
    assert record["RequestLatencyMs"] == 12.5


def test_every_named_metric_appears_at_the_top_level():
    """A metric declared but not valued is dropped by CloudWatch without complaint."""
    record = _emit_to_string(
        metrics={"Requests": 1, "ColdStart": 1},
        dimensions={"Kind": "mcp", "Outcome": "ok"},
    )
    declared = {m["Name"] for m in record["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
    assert declared <= set(record), "a declared metric has no value in the record"


def test_disabled_is_the_default_so_nothing_writes_emf_to_a_terminal(monkeypatch):
    monkeypatch.setattr(telemetry, "ENABLED", False)
    buffer = io.StringIO()
    assert telemetry.emit({"Requests": 1}, stream=buffer) is None
    assert buffer.getvalue() == ""


def test_a_timed_request_records_latency_and_a_successful_outcome(monkeypatch):
    seen = []
    monkeypatch.setattr(telemetry, "emit", lambda *a, **k: seen.append((a, k)))

    with telemetry.timed_request("mcp") as span:
        span["path"] = "/mcp"

    (metrics,), kwargs = seen[0]
    assert metrics["Requests"] == 1
    assert metrics["RequestLatencyMs"] >= 0
    assert kwargs["dimensions"]["Outcome"] == "ok"
    assert kwargs["fields"]["path"] == "/mcp"


def test_a_failing_request_is_still_measured_and_marked_an_error(monkeypatch):
    """A metric that only records successes describes a system that never breaks."""
    seen = []
    monkeypatch.setattr(telemetry, "emit", lambda *a, **k: seen.append((a, k)))

    with pytest.raises(ValueError):
        with telemetry.timed_request("mcp"):
            raise ValueError("boom")

    (metrics,), kwargs = seen[0]
    assert metrics["Requests"] == 1
    assert metrics["RequestLatencyMs"] >= 0
    assert kwargs["dimensions"]["Outcome"] == "error"


def test_the_exception_is_re_raised_not_swallowed():
    """Telemetry must never change what the caller sees."""
    with pytest.raises(KeyError):
        with telemetry.timed_request("mcp"):
            raise KeyError("still mine")


def test_only_the_first_request_in_a_container_counts_as_a_cold_start(monkeypatch):
    """Mixing cold starts into the same distribution makes the p99 a statement
    about container churn rather than about the code."""
    seen = []
    monkeypatch.setattr(telemetry, "emit", lambda *a, **k: seen.append(a[0]))

    with telemetry.timed_request("mcp"):
        pass
    with telemetry.timed_request("mcp"):
        pass

    assert "ColdStart" in seen[0]
    assert "ColdStart" not in seen[1]


def test_a_failed_tool_call_emits_an_error_metric(monkeypatch):
    seen = []
    monkeypatch.setattr(telemetry, "emit", lambda *a, **k: seen.append((a, k)))

    telemetry.record_tool_call("list_products", ok=False, duration_ms=8.0)

    (metrics,), kwargs = seen[0]
    assert metrics["ToolCalls"] == 1
    assert metrics["ToolErrors"] == 1
    assert metrics["ToolLatencyMs"] == 8.0
    assert kwargs["dimensions"] == {"Tool": "list_products"}


def test_a_successful_tool_call_emits_no_error_metric(monkeypatch):
    seen = []
    monkeypatch.setattr(telemetry, "emit", lambda *a, **k: seen.append(a[0]))
    telemetry.record_tool_call("list_products")
    assert "ToolErrors" not in seen[0]


def test_a_model_substitution_names_both_models(monkeypatch):
    """The alarm needs to fire; the dimensions are what make it actionable."""
    seen = []
    monkeypatch.setattr(telemetry, "emit", lambda *a, **k: seen.append((a, k)))

    telemetry.record_model_substitution("openai/gpt-oss-120b", "qwen/qwen3.8-27b")

    (metrics,), kwargs = seen[0]
    assert metrics["ModelSubstituted"] == 1
    assert kwargs["dimensions"]["ConfiguredModel"] == "openai/gpt-oss-120b"
    assert kwargs["dimensions"]["ActualModel"] == "qwen/qwen3.8-27b"


def test_telemetry_failure_never_breaks_the_model_resolver(monkeypatch):
    """The agent must survive a broken metric emitter. It is the least important
    thing in the process and it sits on the path of the most important one."""
    from agent import graph

    monkeypatch.setattr(graph, "_resolved_model", None)
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setattr(graph, "available_models", lambda *a, **k: {"qwen/qwen3.8-27b"})

    def explode(*_a, **_k):
        raise RuntimeError("cloudwatch is having a day")

    monkeypatch.setattr(telemetry, "record_model_substitution", explode)

    assert graph.resolve_model() == "qwen/qwen3.8-27b"
