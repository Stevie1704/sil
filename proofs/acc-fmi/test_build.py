"""Normalization must not reserialize unrelated XML, even when metadata is absent."""
import pytest
from build import normalize_xml_metadata


@pytest.mark.parametrize("date", [b'', b' generationDateAndTime="2026-01-01T00:00:00Z"'])
def test_only_root_identity_attributes_change(date):
    before = (b'<?xml version="1.0"?>\n<!-- instantiationToken="keep" -->\n'
              b'<f:fmiModelDescription xmlns:f="urn:example" instantiationToken = \'old\''
              + date + b' description="a &gt; b">\n'
              b'  <!-- keep --><?keep whitespace?>\n'
              b'  <f:Child instantiationToken="child" generationDateAndTime="keep" />\n'
              b'</f:fmiModelDescription>')
    wanted = before.replace(b"instantiationToken = 'old'", b"instantiationToken = 'new'")
    if date:
        wanted = wanted.replace(date, b'')
    assert normalize_xml_metadata(before, "new") == wanted


def test_missing_token_is_rejected():
    with pytest.raises(ValueError, match="Missing instantiationToken"):
        normalize_xml_metadata(b'<fmiModelDescription/>', "new")
