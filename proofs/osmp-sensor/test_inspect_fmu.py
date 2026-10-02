"""The static compatibility report reads only the model description and archive names."""
from inspect_fmu import describe, runtime_gaps, sil_gaps

FMI2 = """<fmiModelDescription fmiVersion="2.0" modelName="Dummy" guid="{g}"
  generationTool="Tool" generationDateAndTime="2026-10-02T00:00:00Z">
  <CoSimulation modelIdentifier="Dummy" canHandleVariableCommunicationStepSize="true"/>
  <DefaultExperiment startTime="0.0" stepSize="0.020"/>
  <ModelVariables>
    <ScalarVariable name="count" valueReference="12" causality="output" variability="discrete"
      initial="exact"><Integer start="0"/></ScalarVariable>
    <ScalarVariable name="nominalrange" valueReference="0" causality="parameter"
      variability="fixed"><Real start="135.0"/></ScalarVariable>
  </ModelVariables>
</fmiModelDescription>"""


def test_the_interface_is_read_from_the_model_description():
    description = describe(FMI2, ["binaries/linux64/Dummy.so", "modelDescription.xml"])
    assert description["fmi_version"] == "2.0"
    assert description["interfaces"] == {"CoSimulation": {
        "modelIdentifier": "Dummy", "canHandleVariableCommunicationStepSize": "true"}}
    assert description["default_experiment"] == {"startTime": "0.0", "stepSize": "0.020"}
    assert description["platforms"] == ["linux64"]
    assert description["variables"] == [
        {"name": "count", "value_reference": 12, "type": "Integer", "causality": "output",
         "variability": "discrete", "initial": "exact", "start": "0"},
        {"name": "nominalrange", "value_reference": 0, "type": "Real", "causality": "parameter",
         "variability": "fixed", "initial": None, "start": "135.0"}]
    assert description["arrays"] == [] and description["clocks"] == []


def test_fmi_2_and_osmp_pointers_are_named_with_their_issues():
    description = describe(FMI2, ["binaries/linux64/Dummy.so"])
    description["osmp_binary_variables"] = {"In": {}}
    assert sil_gaps(description) == [
        "fmiVersion 2.0: the Importer drives FMI 3.0 only (#191)",
        "OSMP binary variables pass memory addresses in fmi2Integer variables (#244)",
    ]


def test_a_missing_linux_binary_is_a_gap():
    description = describe(FMI2, ["binaries/win64/Dummy.dll"])
    assert "no Linux x86-64 binary (binaries/linux64)" in sil_gaps(description)


def test_a_dynamic_protobuf_is_a_runtime_gap():
    assert runtime_gaps({"needed": ["libprotobuf.so.32", "libc.so.6"]}) == [
        "needs the system libprotobuf.so.32 at run time; a second FMU built this way "
        "aborts in the same process (#233, #244)"]
    assert runtime_gaps({"needed": ["libc.so.6"]}) == []
