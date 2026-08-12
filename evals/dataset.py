"""
The evaluation set: twelve requests, each with the trajectory it should produce.

Every scenario is dated relative to EVAL_TODAY and the database is seeded from
the same date, so the expected tool arguments are exact rather than fuzzy. The
agent is given the same frozen date in its system prompt, which is what makes
"this Saturday" resolvable to a specific ISO date the eval can assert on.

Scenarios are written around the behaviours that actually broke: refusing before
looking anything up, inventing arithmetic, pricing without checking, and writing
to the schedule when only asked a question.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

# A Tuesday. The seed puts 180 bookings on Saturdays inside two weeks, leaves
# later ones empty, and creates no row at all for Mondays.
EVAL_TODAY = date(2026, 9, 1)

SATURDAY, MONDAY = 5, 0


def _next(weekday: int, after_days: int = 0) -> str:
    day = EVAL_TODAY + timedelta(days=after_days)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day.isoformat()


BUSY_SATURDAY = _next(SATURDAY)                    # 70 servings left
FREE_SATURDAY = _next(SATURDAY, after_days=16)     # 250 servings left
CLOSED_MONDAY = _next(MONDAY, after_days=1)        # no capacity row
TOMORROW = (EVAL_TODAY + timedelta(days=1)).isoformat()


@dataclass(frozen=True)
class Scenario:
    id: str
    question: str
    lang: str = "en"
    #: Tools the agent must call for the answer to be trustworthy.
    required_tools: List[str] = field(default_factory=list)
    #: Every question here is about live business data, so answering any of them
    #: without a single lookup is a failure regardless of how right it sounds.
    min_tool_calls: int = 1
    #: At least one of these must be called. For questions where more than one
    #: tool is a legitimate route to the same answer, pinning a single required
    #: tool marks a correct trajectory wrong.
    any_of_tools: List[str] = field(default_factory=list)
    #: Tools it must not call — writes, mostly.
    forbidden_tools: List[str] = field(default_factory=list)
    #: (before, after) pairs the call order must respect.
    order: List[Tuple[str, str]] = field(default_factory=list)
    #: Arguments that must appear on the closest matching call.
    expected_args: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: Case-insensitive substrings the answer must / must not contain.
    must_mention: List[str] = field(default_factory=list)
    must_not_mention: List[str] = field(default_factory=list)
    #: Whether every figure in the answer must trace back to a tool result.
    check_grounding: bool = True


SCENARIOS: List[Scenario] = [
    Scenario(
        id="price-lookup",
        question="What does a churro bar cost per person?",
        required_tools=["list_catalog"],
        forbidden_tools=["place_order"],
        must_mention=["8.00"],
    ),
    Scenario(
        id="availability-open",
        question=f"Do you have room for 60 people on {FREE_SATURDAY}?",
        required_tools=["check_availability"],
        forbidden_tools=["place_order"],
        expected_args={"check_availability": {"day": FREE_SATURDAY}},
    ),
    Scenario(
        id="closed-day-is-not-full",
        question=f"Can you cater 80 people on {CLOSED_MONDAY}?",
        # Either route is correct: quote_catering reports the closure as a
        # blocker, check_availability reports it directly.
        any_of_tools=["check_availability", "quote_catering"],
        forbidden_tools=["place_order"],
        must_mention=["closed"],
        # The distinction the schema goes out of its way to preserve; an agent
        # that reports a closed day as "fully booked" throws it away.
        must_not_mention=["fully booked", "sold out"],
    ),
    Scenario(
        id="over-capacity-reports-and-prices",
        question=f"Can you do catering for 200 people on {BUSY_SATURDAY}?",
        required_tools=["quote_catering"],
        forbidden_tools=["place_order"],
        expected_args={"quote_catering": {"day": BUSY_SATURDAY, "headcount": 200}},
        must_mention=["70"],
    ),
    Scenario(
        id="clean-quote-with-delivery",
        question=(
            f"Quote catering for 80 people on {FREE_SATURDAY}, with delivery, please."
        ),
        required_tools=["quote_catering"],
        forbidden_tools=["place_order"],
        expected_args={
            "quote_catering": {"day": FREE_SATURDAY, "headcount": 80, "delivery": True}
        },
        must_mention=["640.00", "160.00"],
    ),
    Scenario(
        id="under-minimum",
        question=f"We need catering for 20 people on {FREE_SATURDAY}.",
        required_tools=["quote_catering"],
        forbidden_tools=["place_order"],
        must_mention=["50"],
    ),
    Scenario(
        id="short-notice",
        question=f"Can you cater 60 people on {TOMORROW}?",
        required_tools=["quote_catering"],
        forbidden_tools=["place_order"],
        must_mention=["notice"],
    ),
    Scenario(
        id="question-is-not-a-booking",
        question=(
            f"Would you be able to handle 80 people on {FREE_SATURDAY}? "
            "Just checking before I talk to my team."
        ),
        # The write must not fire on a question. quote_catering answers it
        # without touching the schedule.
        forbidden_tools=["place_order"],
        required_tools=["quote_catering"],
    ),
    Scenario(
        id="explicit-booking-quotes-first",
        question=(
            f"Book catering for 80 people on {FREE_SATURDAY} for Acme Ltd, "
            "contact ops@acme.example. I confirm the booking, go ahead."
        ),
        required_tools=["place_order"],
        order=[("quote_catering", "place_order")],
        expected_args={"place_order": {"day": FREE_SATURDAY, "headcount": 80}},
        must_mention=["confirm"],
    ),
    Scenario(
        id="stock-question",
        question="Do we have enough gluten-free churros to serve 30 people?",
        required_tools=["check_stock"],
        forbidden_tools=["place_order"],
    ),
    Scenario(
        id="plausible-but-unlisted-topic",
        question="Are you on Uber Eats or DoorDash?",
        # The regression from the sibling RAG project: an ordinary customer
        # question refused as off-topic without a single lookup. Any tool call
        # counts — the point is that it looked before it answered.
        forbidden_tools=["place_order"],
        must_not_mention=["I can only", "I'm only able"],
        check_grounding=False,
    ),
    Scenario(
        id="spanish",
        question=f"¿Tenéis hueco para 60 personas el {FREE_SATURDAY}?",
        lang="es",
        required_tools=["check_availability"],
        forbidden_tools=["place_order"],
        expected_args={"check_availability": {"day": FREE_SATURDAY}},
    ),
]


def scenarios(only: List[str] = None) -> List[Scenario]:
    if not only:
        return list(SCENARIOS)
    wanted = set(only)
    unknown = wanted - {s.id for s in SCENARIOS}
    if unknown:
        raise ValueError(f"Unknown scenario id(s): {sorted(unknown)}")
    return [s for s in SCENARIOS if s.id in wanted]
