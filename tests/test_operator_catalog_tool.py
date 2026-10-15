# -*- coding: utf-8 -*-

from data_juicer_agents.tools.retrieve import list_operator_catalog


def test_list_operator_catalog_returns_non_empty_operator_descriptions():
    payload = list_operator_catalog()

    assert payload["ok"] is True
    assert payload["returned_count"] > 0
    assert payload["total_count"] >= payload["returned_count"]
    first = payload["operators"][0]
    assert first["operator_name"]
    assert "description" in first
    assert "operator_type" in first
    assert "tags" in first
    assert "parameters" not in first


def test_list_operator_catalog_applies_filters_and_limit():
    payload = list_operator_catalog(op_type="filter", tags=["text"], limit=5)

    assert payload["ok"] is True
    assert payload["returned_count"] <= 5
    assert payload["op_type_filter"] == "filter"
    assert payload["requested_tags"] == ["text"]
    assert payload["operators"]
    for item in payload["operators"]:
        assert item["operator_type"].lower() == "filter"
        assert "text" in [tag.lower() for tag in item["tags"]]


def test_list_operator_catalog_can_include_parameters():
    payload = list_operator_catalog(
        op_type="filter",
        tags=["text"],
        include_parameters=True,
        limit=3,
    )

    assert payload["ok"] is True
    assert payload["include_parameters"] is True
    assert payload["operators"]
    for item in payload["operators"]:
        assert "parameters" in item
        assert isinstance(item["parameters"], list)
