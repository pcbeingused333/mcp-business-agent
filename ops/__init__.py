"""
Business operations domain: storage, rules, and pricing.

Deliberately free of any MCP import. The server in server.py is a thin adapter
over this package, so the logic that decides whether an order can be accepted is
testable without starting a transport or an agent.
"""
