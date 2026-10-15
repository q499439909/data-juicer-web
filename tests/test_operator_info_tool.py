# -*- coding: utf-8 -*-

from data_juicer_agents.tools.retrieve import get_operator_info


def test_get_operator_info_returns_structured_parameter_schema():
    payload = get_operator_info("text_length_filter")

    assert payload["ok"] is True
    assert payload["resolved_name"] == "text_length_filter"
    assert payload["operator_type"] == "filter"
    assert isinstance(payload["tags"], list)
    assert payload["source_path"]
    assert payload["test_path"]
    assert any(item["name"] == "max_len" for item in payload["parameters"])


def test_get_operator_info_resolves_non_canonical_name():
    payload = get_operator_info("TextLengthFilter")

    assert payload["ok"] is True
    assert payload["resolved_name"] == "text_length_filter"
    assert payload["resolved"] is True
    assert payload["exact_match"] is False


def test_get_operator_info_unknown_operator_returns_structured_error():
    payload = get_operator_info("definitely_missing_operator_xyz")

    assert payload["ok"] is False
    assert payload["error_type"] == "operator_not_found"
