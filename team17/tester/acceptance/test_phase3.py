from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener

import pytest

from acceptance.cases import Phase3Checks
from acceptance.client import NoRedirect


class TestRemotePhase3(Phase3Checks):
    pass


def test_ac15_plain_http_is_refused_or_redirected_to_https(pytestconfig):
    base = urlsplit(pytestconfig.getoption("--base-url"))
    if base.scheme != "https":
        pytest.skip(
            "HTTPS enforcement requires the deployed HTTPS target, not loopback development"
        )
    # The public probe carries no credentials and never follows the redirect.
    url = urlunsplit(("http", base.netloc, base.path.rstrip("/") + "/health", "", ""))
    try:
        response = build_opener(NoRedirect()).open(Request(url, method="GET"), timeout=30)
    except HTTPError as error:
        response = error
    except URLError:
        return
    with response:
        if response.code in (400, 403, 404, 405, 426):
            return
        assert response.code in (301, 302, 307, 308)
        assert urlsplit(response.headers.get("Location", "")).scheme == "https"
