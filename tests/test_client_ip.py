from starlette.requests import Request

from app.utils.request import get_client_ip


def _request(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
            "client": ("10.0.0.9", 1234),
        }
    )


def test_website_proxy_client_ip_wins_over_forwarded_for():
    # Railway's edge replaces X-Forwarded-For with the website server's address.
    request = _request({"x-client-ip": "1.2.3.4", "x-forwarded-for": "34.0.0.1"})
    assert get_client_ip(request) == "1.2.3.4"


def test_forwarded_for_then_socket_address():
    assert get_client_ip(_request({"x-forwarded-for": "5.6.7.8, 10.0.0.1"})) == "5.6.7.8"
    assert get_client_ip(_request({})) == "10.0.0.9"
