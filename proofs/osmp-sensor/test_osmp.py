"""OSMP binary variables: a pointer in two fmi2Integer halves plus a size."""
import pytest
from osmp import binary_variables, decode_pointer, encode_pointer


def test_a_64_bit_address_splits_into_signed_halves():
    lo, hi = encode_pointer(0x00007F12_80000004)
    assert (lo, hi) == (-2147483644, 0x7F12)
    assert decode_pointer(lo, hi) == 0x00007F12_80000004


@pytest.mark.parametrize("address", [0, 1, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF_FFFFFFFF])
def test_every_address_survives_the_round_trip(address):
    assert decode_pointer(*encode_pointer(address)) == address


ANNOTATION = ('<Annotations><Tool name="net.pmsf.osmp" xmlns:osmp="http://xsd.pmsf.net/'
              'OSISensorModelPackaging"><osmp:osmp-binary-variable name="{name}" role="{role}" '
              'mime-type="application/x-open-simulation-interface; type=SensorView; '
              'version=3.8.0"/></Tool></Annotations>')


def model(*roles):
    variables = "".join(
        f'<ScalarVariable name="In.{role}" valueReference="{vr}" causality="input">'
        f'<Integer start="0"/>{ANNOTATION.format(name="In", role=role)}</ScalarVariable>'
        for vr, role in enumerate(roles))
    return (f'<fmiModelDescription fmiVersion="2.0"><ModelVariables>{variables}'
            '<ScalarVariable name="count" valueReference="9" causality="output"><Integer/>'
            '</ScalarVariable></ModelVariables></fmiModelDescription>')


def test_the_three_roles_group_into_one_binary_variable():
    assert binary_variables(model("base.lo", "base.hi", "size")) == {"In": {
        "causality": "input",
        "mime_type": "application/x-open-simulation-interface; type=SensorView; version=3.8.0",
        "value_references": {"base.lo": 0, "base.hi": 1, "size": 2}}}


def test_a_binary_variable_without_all_roles_is_refused():
    with pytest.raises(ValueError, match="In"):
        binary_variables(model("base.lo", "size"))
