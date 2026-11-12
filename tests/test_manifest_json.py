"""The shared reader of Manifest JSON refuses a repeated key (#394)."""

import pytest

from sil import manifest_json


def test_a_document_without_a_repeat_loads():
    assert manifest_json.loads('{"a": {"b": 1}, "c": [{"d": 2}]}') == \
        {"a": {"b": 1}, "c": [{"d": 2}]}


@pytest.mark.parametrize("text, message", [
    ('{"a": 1, "a": 2}', "object '' repeats key 'a'"),
    ('{"p": {"x": 1, "x": 2}}', "object '/p' repeats key 'x'"),
    ('{"c": [{}, {"k/~": 1, "k/~": 1}]}',
     "object '/c/1' repeats key 'k/~'"),
])
def test_a_repeated_key_at_any_level_is_refused_with_its_object(text,
                                                                message):
    with pytest.raises(manifest_json.RepeatedKey) as raised:
        manifest_json.loads(text)

    assert str(raised.value) == message


def test_a_repeated_key_is_a_value_error():
    with pytest.raises(ValueError):
        manifest_json.loads('{"a": 1, "a": 2}')


def test_bytes_with_a_byte_order_mark_load():
    assert manifest_json.loads(b'\xef\xbb\xbf{"a": 1}') == {"a": 1}
