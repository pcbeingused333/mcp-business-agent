"""
Scoring a tool trajectory. Pure functions over captured data — no LLM, no network.

The grounding checks are the point of this file. Everything else here (did it
call the right tools, in the right order, with the right arguments) is standard
agent evaluation. Grounding is what catches the failures that *look* like
successes:

  - "the request is 5 servings short" when the tools said 70 and 80
  - a $560.00 quote the agent produced without calling the pricing tool

Both are deterministic to detect: pull every figure out of the answer and check
it appears in a tool result, in the question, or in the arguments the agent
itself sent. No judge model, no drift, no API key.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

# $1,234.56 — the format every money field in this project renders.
MONEY = re.compile(r"\$\s?([\d,]+\.\d{2})")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
# A number written as a percentage is a policy statement ("25 % deposit"), not a
# figure derived from a tool result, so it is excluded from the count check.
PERCENT = re.compile(r"\d+(?:\.\d+)?\s*%")
# "1." / "2)" at the start of a line is a list marker, not a quantity. Models
# offer numbered options constantly, and counting those as invented figures
# fails answers for their formatting.
LIST_MARKER = re.compile(r"^[ \t>*-]*\d{1,2}[.)]\s", re.MULTILINE)
NUMBER = re.compile(r"\d[\d,]*")

# Models render dates with typographic hyphens — 2026‑09‑19 with U+2011, not
# U+002D. Left unnormalised, the ISO-date pattern misses them and every date
# fragment shows up as an invented quantity. Found by the eval's own output
# looking implausible: "2026 appears in no tool result", ten scenarios running.
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—−"), "-")


def normalise_text(text: str) -> str:
    return text.translate(_DASHES)


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: Dict[str, Any]


@dataclass(frozen=True)
class Trajectory:
    """One run of the agent: what it called, what came back, what it said."""

    calls: List[ToolCall]
    results: List[str]
    answer: str
    error: Optional[str] = None

    @property
    def tool_names(self) -> List[str]:
        return [call.name for call in self.calls]

    def calls_to(self, name: str) -> List[ToolCall]:
        return [call for call in self.calls if call.name == name]


def normalise_money(value: str) -> str:
    """'1,234.56' and '1234.56' are the same amount."""
    return value.replace(",", "").lstrip("0") or "0"


def money_in(text: str) -> Set[str]:
    return {normalise_money(match) for match in MONEY.findall(normalise_text(text))}


def counts_in(text: str) -> Set[int]:
    """
    Integers a reader would take as a quantity.

    Money, dates, years and percentages are removed first — '$640.00' is checked
    separately, '2026-09-19' is not a claim about how many servings are left, and
    '25 %' is policy rather than a figure derived from a tool result.

    Numbers are matched with a plain pattern rather than lookarounds, because a
    lookahead excluding a following comma silently skipped every number in a
    JSON tool result ("on_hand": 40,) while catching the same number in prose —
    which reported grounded figures as invented.
    """
    cleaned = normalise_text(text)
    for pattern in (LIST_MARKER, MONEY, ISO_DATE, YEAR, PERCENT):
        cleaned = pattern.sub(" ", cleaned)
    return {int(match.replace(",", "")) for match in NUMBER.findall(cleaned)}


def _corpus(trajectory: Trajectory, question: str) -> str:
    """Everything the agent legitimately saw or sent."""
    parts = [question, *trajectory.results]
    for call in trajectory.calls:
        parts.append(" ".join(str(value) for value in call.args.values()))
    return "\n".join(parts)


def ungrounded_money(trajectory: Trajectory, question: str) -> Set[str]:
    """
    Money in the answer that no tool ever returned.

    This is the check that catches a quote produced from memory. It stays
    correct even when the invented figure happens to be right, because the
    question is whether the agent *looked it up*, not whether it guessed well.
    """
    return money_in(trajectory.answer) - money_in(_corpus(trajectory, question))


def date_components(text: str) -> Set[int]:
    """
    The year, month and day of every ISO date in the text.

    A date the tools returned licences its own parts: an agent writing
    "Saturday 19 September" or "el sábado 19" is repeating 2026-09-19, not
    inventing the number 19. Stripping ISO dates alone does not cover the
    long-form renderings, and this stays language-independent — which matters,
    because the Spanish scenario is where it first showed up.
    """
    parts: Set[int] = set()
    for match in ISO_DATE.findall(normalise_text(text)):
        parts.update(int(piece) for piece in match.split("-"))
    return parts


def ungrounded_counts(trajectory: Trajectory, question: str, floor: int = 2) -> Set[int]:
    """
    Quantities in the answer that appear nowhere in the inputs.

    `floor` skips small numbers — list markers, "2 options", "step 1" — which are
    presentation rather than claims about the business. A computed shortfall like
    "5 servings short" sits above it and is caught.
    """
    corpus = _corpus(trajectory, question)
    seen = counts_in(corpus) | date_components(corpus)
    return {n for n in counts_in(trajectory.answer) if n >= floor and n not in seen}


# ---- tool selection, ordering, arguments ----


def missing_tools(trajectory: Trajectory, required: Iterable[str]) -> Set[str]:
    return set(required) - set(trajectory.tool_names)


def forbidden_used(trajectory: Trajectory, forbidden: Iterable[str]) -> Set[str]:
    """A write tool called when the scenario says it must not be."""
    return set(forbidden) & set(trajectory.tool_names)


def order_respected(trajectory: Trajectory, before: str, after: str) -> bool:
    """
    Whether every call to `after` follows at least one call to `before`.

    The rule that matters here is quote-before-book: the agent must not write to
    the schedule until it has checked the request is allowed.
    """
    names = trajectory.tool_names
    if after not in names:
        return True
    if before not in names:
        return False
    return names.index(before) < names.index(after)


def args_match(trajectory: Trajectory, tool: str, expected: Dict[str, Any]) -> List[str]:
    """
    Mismatches between the expected arguments and the closest actual call.

    Only the keys in `expected` are compared, so a scenario can pin the date
    without caring which package the agent chose. The call that matches the most
    keys is the one reported on, so an agent that explored alternatives is judged
    on its best attempt rather than its first.
    """
    calls = trajectory.calls_to(tool)
    if not calls:
        return [f"{tool} was never called"]

    def score(call: ToolCall) -> int:
        return sum(1 for k, v in expected.items() if str(call.args.get(k)) == str(v))

    best = max(calls, key=score)
    return [
        f"{tool}.{key}: expected {value!r}, got {best.args.get(key)!r}"
        for key, value in expected.items()
        if str(best.args.get(key)) != str(value)
    ]


# ---- the per-scenario verdict ----


@dataclass
class ScenarioResult:
    scenario_id: str
    trajectory: Trajectory
    failures: List[str] = field(default_factory=list)
    checks: int = 0

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def score(self) -> float:
        if not self.checks:
            return 1.0
        return max(0.0, (self.checks - len(self.failures)) / self.checks)


def summarise(results: Sequence[ScenarioResult]) -> Dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    by_kind: Dict[str, int] = {}
    for result in results:
        for failure in result.failures:
            kind = failure.split(":", 1)[0]
            by_kind[kind] = by_kind.get(kind, 0) + 1
    return {
        "scenarios": total,
        "passed": passed,
        "pass_rate": passed / total if total else 0.0,
        "mean_score": sum(r.score for r in results) / total if total else 0.0,
        "failures_by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
    }
