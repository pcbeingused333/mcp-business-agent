"""
A LangGraph agent that reaches the business through the MCP server.

The agent owns no business logic. Everything it can do, it does by calling a
tool the server exposes — which is the point: swap the server and the same agent
works against a different business.
"""
