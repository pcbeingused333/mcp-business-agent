"""
Metrics from log lines, using CloudWatch's Embedded Metric Format (EMF).

## Why EMF and not `put_metric_data`

A `put_metric_data` call is an HTTP request to CloudWatch on the request path.
That buys three things nobody wants in a Lambda: latency added to every
invocation, a second thing that can fail while serving a user, and a per-call
charge. EMF avoids all three — you write one JSON line to stdout, which the
Lambda runtime already ships to CloudWatch Logs, and CloudWatch parses the
`_aws` block out of it and creates the metrics itself. No extra call, no extra
failure mode, and the cost is the log line you were writing anyway.

The trade-off is honesty about latency: metrics appear when the log batch is
delivered, not the instant they are recorded. For "what is the p99 this week"
that is irrelevant. For a trading system it would not be.

## What is measured, and why these

`RequestLatencyMs` is the only way to answer "what is the p99", which is the
question that separates having deployed something from operating it.
`ColdStart` is separated out because a cold start is several seconds and mixing
it into the same distribution makes the p99 a statement about container churn
rather than about the code.

`Outcome` is a dimension rather than a separate metric so that error rate is a
ratio of the same series, and `ModelSubstituted` exists because the fallback
added in `agent/graph.py` keeps the demo alive when a pinned model is retired —
and a fallback nobody is told about is just a slower way of not knowing.

## Dimensions are deliberately few

Every distinct combination of dimension values is a separate custom metric, and
custom metrics are the part of CloudWatch that costs money. Tool names are
bounded and known; anything unbounded — a request id, a user input — must never
become a dimension.
"""
import json
import os
import sys
import time
from contextlib import contextmanager
from typing import Dict, Iterator, Optional

NAMESPACE = os.environ.get("METRICS_NAMESPACE", "BusinessOpsMCP")

# Off by default so that unit tests, the CLI and anyone running this locally do
# not write EMF blocks into their terminal. Lambda sets it in Terraform.
ENABLED = os.environ.get("EMIT_METRICS", "").lower() in ("1", "true", "yes")

_cold = True


def _now_ms() -> int:
    return int(time.time() * 1000)


def emit(
    metrics: Dict[str, float],
    units: Optional[Dict[str, str]] = None,
    dimensions: Optional[Dict[str, str]] = None,
    fields: Optional[Dict[str, object]] = None,
    stream=None,
) -> Optional[dict]:
    """Write one EMF record. Returns the record, or None when disabled.

    Returning the record rather than nothing is what makes this testable without
    capturing stdout, and the tests assert on the shape CloudWatch requires
    rather than on the text that happens to be printed.
    """
    if not ENABLED:
        return None

    units = units or {}
    dimensions = dimensions or {}
    record = {
        "_aws": {
            "Timestamp": _now_ms(),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    # One dimension set. A list of sets multiplies the number of
                    # custom metrics, and the bill with it.
                    "Dimensions": [sorted(dimensions)] if dimensions else [[]],
                    "Metrics": [
                        {"Name": name, "Unit": units.get(name, "None")}
                        for name in metrics
                    ],
                }
            ],
        },
        **dimensions,
        **metrics,
        **(fields or {}),
    }
    print(json.dumps(record), file=stream or sys.stdout, flush=True)
    return record


@contextmanager
def timed_request(kind: str = "mcp") -> Iterator[dict]:
    """Time one invocation and emit latency, outcome and cold-start.

    The context manager yields a dict the caller can annotate — `tool`, say —
    and emits on the way out **including when the body raises**, because the
    latency of a request that failed is exactly the number you want when
    something is wrong, and a metric that only records successes describes a
    system that never breaks.
    """
    global _cold

    was_cold = _cold
    _cold = False
    started = time.perf_counter()
    context: dict = {"outcome": "ok"}
    try:
        yield context
    except BaseException:
        context["outcome"] = "error"
        raise
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        metrics = {"RequestLatencyMs": round(elapsed_ms, 2), "Requests": 1}
        if was_cold:
            metrics["ColdStart"] = 1
        emit(
            metrics,
            units={"RequestLatencyMs": "Milliseconds", "Requests": "Count",
                   "ColdStart": "Count"},
            dimensions={"Kind": kind, "Outcome": context["outcome"]},
            fields={k: v for k, v in context.items() if k != "outcome"},
        )


def record_tool_call(tool: str, ok: bool = True, duration_ms: Optional[float] = None) -> None:
    """One tool invocation. `tool` is bounded — it is a name from the server."""
    metrics: Dict[str, float] = {"ToolCalls": 1}
    units = {"ToolCalls": "Count"}
    if not ok:
        metrics["ToolErrors"] = 1
        units["ToolErrors"] = "Count"
    if duration_ms is not None:
        metrics["ToolLatencyMs"] = round(duration_ms, 2)
        units["ToolLatencyMs"] = "Milliseconds"
    emit(metrics, units=units, dimensions={"Tool": tool})


def record_model_substitution(configured: str, actual: str) -> None:
    """The pinned model was retired and the fallback took over.

    This is the metric behind the alarm: the demo stays up, and the alarm is how
    anyone finds out that the numbers in the README now describe a model that is
    no longer running.
    """
    emit(
        {"ModelSubstituted": 1},
        units={"ModelSubstituted": "Count"},
        dimensions={"ConfiguredModel": configured, "ActualModel": actual},
    )


def reset_cold_start_for_tests() -> None:
    """Tests share a process; the cold-start flag would otherwise fire once ever."""
    global _cold
    _cold = True
