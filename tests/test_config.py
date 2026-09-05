from __future__ import annotations

import pytest
from pydantic import ValidationError

from avito.config import Browser, Settings, Watch, Web


def test_proxy_is_split_into_playwright_options():
    plain = Browser(proxy="http://proxy.example.com:3128")
    assert plain.proxy_options() == {"server": "http://proxy.example.com:3128"}

    withauth = Browser(proxy="http://user:secret@proxy.example.com:3128")
    assert withauth.proxy_options() == {"server": "http://proxy.example.com:3128",
                                        "username": "user", "password": "secret"}

    noport = Browser(proxy="socks5://proxy.example.com")
    assert noport.proxy_options() == {"server": "socks5://proxy.example.com"}

    assert Browser().proxy_options() is None


@pytest.mark.parametrize(("factory", "values"), [
    (Browser, {"timeout_seconds": 0}),
    (Browser, {"deadline_seconds": 10}),
    (Browser, {"max_pages": 0}),
    (Browser, {"proxy": "not-a-proxy"}),
    (Browser, {"timezone": "Nowhere/Never"}),
    (Watch, {"max_per_cycle": 0}),
    (Watch, {"backoff_seconds": 300, "max_backoff_seconds": 30}),
    (Web, {"port": 99999}),
    (Settings, {"log_level": "LOUD"}),
    (Settings, {"control": ["../bad"]}),
])
def test_runtime_settings_are_validated(factory, values):
    with pytest.raises(ValidationError):
        factory(**values)
