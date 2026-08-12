"""
AWS Lambda entry point: the MCP server behind a Function URL.

The result is a **remote MCP server** — a URL any MCP client can connect to,
including Claude Desktop. Nothing about the tools changes; only where they run
and what they read from.

## Two things Lambda forces

**Stateless sessions.** The MCP streamable-HTTP transport can keep a session
across requests, and Lambda cannot: consecutive requests may land in different
execution environments with no shared memory. `stateless_http=True` tells the
transport to treat every request as self-contained, which is the only correct
setting here — a sticky session would work in testing, when one warm container
serves everything, and break under any real concurrency.

**Buffered responses.** `json_response=True` returns a single JSON body instead
of a server-sent event stream. Function URLs can stream, but only in the
`RESPONSE_STREAM` invoke mode, and Mangum does not speak it. JSON responses work
in the default buffered mode and are what a request/response tool call needs
anyway.

## Cold starts

The store is constructed at module import, not per request, so a warm container
reuses the boto3 client and its connection pool. Importing at module scope is
what makes the second and later invocations cheap.
"""
import json
import logging
import os

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

from ops import store  # noqa: E402
from ops.backends.dynamo import DynamoStore  # noqa: E402

# Once per container, not once per request.
store.use(DynamoStore(os.environ.get("OPS_TABLE", "business-ops")))

import server as srv  # noqa: E402  — imported after the store is installed

# MCP's transport layer rejects unknown Host headers to prevent DNS rebinding —
# a browser on a victim's machine resolving an attacker domain to this server and
# calling tools with the victim's network position. It is on by default and it
# returns 421, which is easy to misread as a routing problem.
#
# The Function URL's hostname is only known after Terraform creates it, so it
# arrives as an environment variable that Terraform sets from its own output.
# ALLOWED_HOSTS unset means the protection stays on with nothing allowed, which
# fails closed: a misconfigured deployment refuses requests rather than
# accepting any Host.
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

_hosts = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]

_app = srv.server.streamable_http_app(
    streamable_http_path=os.environ.get("MCP_PATH", "/mcp"),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(_hosts),
        allowed_hosts=_hosts,
        allowed_origins=_hosts,
    ),
)

class RunLifespanOnce:
    """
    ASGI wrapper that lets the lifespan run exactly once per container.

    Mangum runs the ASGI lifespan on *every* invocation. MCP's
    StreamableHTTPSessionManager refuses to start twice — "run() can only be
    called once per instance" — so the first request in a warm container
    succeeded and every later one failed. A single-request test cannot see it,
    which is how it nearly shipped.

    After the first startup this answers the lifespan protocol itself, so Mangum
    is satisfied and the inner app is never asked to start again. Shutdown is
    also swallowed: a Lambda container is frozen and eventually killed, never
    gracefully drained, and forwarding a shutdown between invocations would tear
    down the session manager the next request needs.
    """

    def __init__(self, app):
        self.app = app
        self.started = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan":
            return await self.app(scope, receive, send)

        if self.started:
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        self.started = True

        async def receive_without_shutdown():
            message = await receive()
            # Swallow the shutdown Mangum sends at the end of an invocation so
            # the session manager stays up for the next request.
            while message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                message = await receive()
            return message

        return await self.app(scope, receive_without_shutdown, send)


try:
    from mangum import Mangum

    _asgi = Mangum(RunLifespanOnce(_app), lifespan="auto")
except ImportError:  # pragma: no cover - only in environments without mangum
    _asgi = None


def _seed(event) -> dict:
    """
    One-off table seeding, invoked directly rather than over HTTP.

    Deliberately not reachable through the Function URL: seeding rewrites the
    catalog and the whole booking calendar, and an endpoint that resets the
    business is not something to leave exposed on a public URL.
    """
    days = int(event.get("days", 60))
    store.current().seed(days=days)
    log.info("seeded %s days", days)
    return {"seeded": True, "days": days}


def handler(event, context):
    """
    Function URL requests go to the MCP app; direct invocations do admin work.

    A Function URL request always carries `requestContext`, so the two are
    distinguishable without a second Lambda or a magic path.
    """
    if isinstance(event, dict) and event.get("action") == "seed":
        return _seed(event)

    if _asgi is None:
        raise RuntimeError("mangum is not installed; this build cannot serve HTTP")

    if "requestContext" not in (event or {}):
        # A bare invocation that is not an admin action is almost always a
        # misconfigured test payload; say so instead of handing it to the ASGI
        # adapter, which fails with something far less obvious.
        return {
            "error": "Unrecognised event. Use the Function URL for MCP, "
                     'or invoke with {"action": "seed"}.',
            "received_keys": sorted((event or {}).keys()),
        }

    return _asgi(event, context)


def health(event, context):
    """Cheap readiness check: proves the table is reachable, not just that Python started."""
    try:
        store.current().initialise()
        products = len(store.current().list_products())
        return {"statusCode": 200, "body": json.dumps({"ok": True, "products": products})}
    except Exception as exc:  # noqa: BLE001 — the point is to report, not raise
        log.exception("health check failed")
        return {"statusCode": 503, "body": json.dumps({"ok": False, "error": str(exc)})}
