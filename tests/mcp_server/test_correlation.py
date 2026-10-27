from argus.mcp_server.correlation import (
    current_http_request,
    extract_caller_token,
    extract_confirm_writes,
    extract_correlation_headers,
)


class _FakeRequest:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


class _FakeRequestContext:
    def __init__(self, request):
        self.request = request


class _FakeServerWithContext:
    def __init__(self, request):
        self.request_context = _FakeRequestContext(request)


class _FakeServerNoContext:
    @property
    def request_context(self):
        raise LookupError("not inside a request")


def test_current_http_request_returns_request_when_present():
    fake_request = _FakeRequest({})
    server = _FakeServerWithContext(fake_request)
    assert current_http_request(server) is fake_request


def test_current_http_request_returns_none_outside_a_request():
    server = _FakeServerNoContext()
    assert current_http_request(server) is None


def test_current_http_request_returns_none_for_stdio_transport():
    # stdio calls still enter a request context, but message_metadata.request_context
    # is only ever populated by the HTTP transports -- .request is None there.
    server = _FakeServerWithContext(None)
    assert current_http_request(server) is None


def test_extract_correlation_headers_returns_empty_dict_for_none_request():
    assert extract_correlation_headers(None) == {}


def test_extract_correlation_headers_extracts_known_headers():
    request = _FakeRequest(
        {
            "x-librechat-conversation-id": "conv-1",
            "x-librechat-message-id": "msg-1",
            "x-librechat-user-id": "user-1",
        }
    )
    assert extract_correlation_headers(request) == {
        "conversation_id": "conv-1",
        "message_id": "msg-1",
        "user_id": "user-1",
    }


def test_extract_correlation_headers_omits_missing_headers():
    request = _FakeRequest({"x-librechat-conversation-id": "conv-1"})
    assert extract_correlation_headers(request) == {"conversation_id": "conv-1"}


def test_extract_correlation_headers_omits_empty_placeholder_values():
    # LibreChat substitutes missing template fields with an empty string
    # rather than omitting the header entirely -- must not turn that into a
    # literal "" conversation_id log label.
    request = _FakeRequest({"x-librechat-conversation-id": ""})
    assert extract_correlation_headers(request) == {}


def test_caller_token_is_read_from_its_own_header():
    assert extract_caller_token(_FakeRequest({"x-argus-user-token": "eyJabc"})) == "eyJabc"
    assert extract_caller_token(_FakeRequest({"x-argus-user-token": "Bearer eyJabc"})) == "eyJabc"


def test_no_header_an_empty_one_or_an_unresolved_placeholder_is_no_token():
    # LibreChat leaves the template as-is when it has no token to put in it.
    assert extract_caller_token(_FakeRequest({})) is None
    assert extract_caller_token(_FakeRequest({"x-argus-user-token": "  "})) is None
    assert extract_caller_token(_FakeRequest({"x-argus-user-token": "{{LIBRECHAT_OPENID_ACCESS_TOKEN}}"})) is None
    assert extract_caller_token(None) is None


def test_the_caller_token_is_not_a_correlation_field():
    # Correlation fields are bound to every log line; a credential must never be.
    assert extract_correlation_headers(_FakeRequest({"x-argus-user-token": "eyJabc"})) == {}


def test_confirm_writes_is_opt_in_by_header():
    assert extract_confirm_writes(_FakeRequest({"x-argus-confirm-writes": "true"})) is True
    assert extract_confirm_writes(_FakeRequest({"x-argus-confirm-writes": "1"})) is True
    assert extract_confirm_writes(_FakeRequest({})) is False
    assert extract_confirm_writes(_FakeRequest({"x-argus-confirm-writes": "no"})) is False
    assert extract_confirm_writes(None) is False
