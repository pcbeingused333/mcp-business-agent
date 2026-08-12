"""
Trajectory evaluation for the agent.

Answer-level scoring asks "was the reply right?". That is not enough for an
agent: a reply can be right by luck — priced from memory, correct this time —
and it can be wrong in a way no reader notices, like a confident weekday that
does not match the date. Both happened here, and neither would fail a
final-answer check.

What this package scores instead is the *trajectory*: which tools were called,
in what order, with which arguments, and whether every figure in the answer can
be traced back to something a tool actually returned.
"""
