"""Absent, non-object and ambiguous fields must not mutate unrelated values."""

import copy

import pytest

from openmaskit.masking.rules import (
    MaskingRule, delete_nested_value, get_nested_value, set_nested_value,
    walk_and_delete, walk_and_mask,
)


@pytest.mark.parametrize("data, path", [
    ([], "author"),
    ("author", "author"),
    ({"message": 7}, "message.author"),
    ({"message": ["synthetic"]}, "message.author"),
    ({"message": {"subject": "synthetic"}}, "message.author"),
    ({"message": {"subject": "synthetic"}}, "message.details.author"),
])
def test_single_object_paths_leave_unresolvable_shapes_unchanged(data, path):
    original = copy.deepcopy(data)
    assert get_nested_value(data, path) is None
    assert not set_nested_value(data, path, "person_1")
    assert not delete_nested_value(data, path)
    assert data == original


@pytest.mark.parametrize("operation", [walk_and_mask, walk_and_delete])
def test_fanout_uses_path_segments_and_skips_missing_fields(operation):
    data = {"messages": [None, 12, {}, {"subject": "Keep"},
        {"author": "Synthetic person", "author.name": "Keep literal"}],
        "messages.author": "Keep dotted", "author": "Keep outside path"}
    if operation is walk_and_mask:
        count = operation(data, "messages.author", lambda _: "person_1")
        assert data["messages"][-1]["author"] == "person_1"
    else:
        count = operation(data, "messages.author")
        assert "author" not in data["messages"][-1]
    assert count == 1
    assert data["messages"][:4] == [None, 12, {}, {"subject": "Keep"}]
    assert data["messages"][-1]["author.name"] == "Keep literal"
    assert data["messages.author"] == "Keep dotted"
    assert data["author"] == "Keep outside path"


def test_default_alias_prefix_uses_the_terminal_field_without_changing_the_path():
    rule = MaskingRule("read", "messages.author")
    assert rule.effective_prefix == "_masked_author"
    assert rule.field_path == "messages.author"
    assert rule.matches_tool("read") and not rule.matches_tool("send")
