from __future__ import annotations

import pytest

from omnihub.core.result import NEED_SETUP, OmniHubError
from omnihub.core.template import resolve_argv, resolve_templates


class FakeCtx:
    def __init__(self, secrets=None):
        self.secrets = secrets or {}

    def secret(self, name):
        return self.secrets.get(name)


def test_resolve_full_placeholder_keeps_native_type():
    value = resolve_templates("${limit}", {"limit": 3}, FakeCtx())
    assert value == 3
    assert isinstance(value, int)


def test_resolve_embedded_placeholder_stringifies():
    value = resolve_templates("prefix-${query}", {"query": "x"}, FakeCtx())
    assert value == "prefix-x"


def test_resolve_recurses_into_dicts_and_lists():
    value = resolve_templates(
        {"q": "${query}", "args": ["${limit}", "static"]},
        {"query": "x", "limit": 2},
        FakeCtx(),
    )
    assert value == {"q": "x", "args": [2, "static"]}


def test_secret_placeholder_uses_context():
    ctx = FakeCtx({"TOKEN": "abc"})
    assert resolve_templates("Bearer ${secret:TOKEN}", {}, ctx) == "Bearer abc"


def test_missing_secret_is_need_setup():
    with pytest.raises(OmniHubError) as info:
        resolve_templates("${secret:TOKEN}", {}, FakeCtx())
    assert info.value.code == NEED_SETUP


def test_unknown_parameter_is_a_manifest_error():
    with pytest.raises(OmniHubError) as info:
        resolve_templates("${nope}", {}, FakeCtx())
    assert info.value.code == NEED_SETUP


def test_resolve_argv_stringifies_all_parts():
    assert resolve_argv(["echo", "${limit}"], {"limit": 3}, FakeCtx()) == ("echo", "3")


def test_urlencode_filter_escapes_query_values():
    assert (
        resolve_templates(
            "https://api.example/search?q=${query|urlencode}&limit=${limit}",
            {"query": "a b&c=d/中", "limit": 3},
            FakeCtx(),
        )
        == "https://api.example/search?q=a+b%26c%3Dd%2F%E4%B8%AD&limit=3"
    )


def test_filters_apply_in_order_and_stringify():
    assert (
        resolve_templates("${query|strip|upper|urlencode}", {"query": "  a b  "}, FakeCtx())
        == "A+B"
    )


def test_unknown_filter_is_manifest_error():
    with pytest.raises(OmniHubError) as info:
        resolve_templates("${query|rot13}", {"query": "x"}, FakeCtx())
    assert info.value.code == NEED_SETUP
