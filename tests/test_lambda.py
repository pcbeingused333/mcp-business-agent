"""
The Lambda handler, against a mocked table. No AWS account, no Docker.

The load-bearing test here is the one that sends *more than one* request. Every
single-request check passed while the second request in a warm container failed,
which is how the bug nearly shipped.
"""
import json

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
pytest.importorskip("mangum")

from moto import mock_aws  # noqa: E402

TABLE = "lambda-test"


@pytest.fixture
def lam(monkeypatch):
    monkeypatch.setenv("OPS_TABLE", TABLE)
    monkeypatch.setenv("ALLOWED_HOSTS", "x.lambda-url.us-east-1.on.aws")
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                       {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        import importlib

        import lambda_handler
        module = importlib.reload(lambda_handler)
        module.handler({"action": "seed", "days": 60}, None)
        yield module


def _post(module, body, host="x.lambda-url.us-east-1.on.aws"):
    event = {
        "version": "2.0", "rawPath": "/mcp", "rawQueryString": "",
        "headers": {"content-type": "application/json",
                    "accept": "application/json, text/event-stream", "host": host},
        "requestContext": {"http": {"method": "POST", "path": "/mcp",
                                    "protocol": "HTTP/1.1", "sourceIp": "1.2.3.4"},
                           "requestId": "r", "stage": "x", "apiId": "a",
                           "domainName": "d", "timeEpoch": 0, "accountId": "1"},
        "body": json.dumps(body), "isBase64Encoded": False,
    }
    response = module.handler(event, None)
    raw = response["body"].strip().splitlines()[-1].replace("data: ", "")
    try:
        return response["statusCode"], json.loads(raw)
    except json.JSONDecodeError:
        return response["statusCode"], raw


def test_initialize_returns_the_server_identity(lam):
    status, payload = _post(lam, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "probe", "version": "1"}},
    })
    assert status == 200
    assert payload["result"]["serverInfo"]["name"] == "business-ops"


def test_repeated_requests_in_a_warm_container_all_succeed(lam):
    """
    The regression that a single-request test cannot see.

    Mangum runs the ASGI lifespan per invocation and MCP's session manager
    refuses to start twice, so request one succeeded and request two failed with
    "run() can only be called once per instance".
    """
    for n in range(1, 4):
        status, payload = _post(lam, {"jsonrpc": "2.0", "id": n, "method": "tools/list"})
        assert status == 200, f"request {n} failed: {payload}"
        assert len(payload["result"]["tools"]) == 6


def test_a_tool_call_reaches_dynamodb(lam):
    """End to end: HTTP event in, MCP dispatch, DynamoDB read, JSON-RPC out."""
    status, payload = _post(lam, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_catalog", "arguments": {"category": "catering"}},
    })
    assert status == 200
    assert "BAR-2CHOC" in json.dumps(payload)


def test_an_unlisted_host_is_rejected(lam):
    """DNS-rebinding protection stays on; a wrong Host must not be served."""
    status, _ = _post(lam, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                      host="evil.example.com")
    assert status == 421


def test_seeding_is_a_direct_invocation_not_an_http_route(lam):
    assert lam.handler({"action": "seed", "days": 10}, None)["seeded"] is True


def test_the_health_check_proves_the_table_is_reachable(lam):
    body = json.loads(lam.health({}, None)["body"])
    assert body["ok"] is True and body["products"] == 11


def test_an_unrecognised_event_is_explained(lam):
    assert "Unrecognised event" in lam.handler({"foo": 1}, None)["error"]
