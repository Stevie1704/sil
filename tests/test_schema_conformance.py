"""Independent byte examples exercised by authoring, native code and Recording.

The fixture hex and offsets are intentional literals, not generated from type
metadata. C++ kernel type/numeric tables remain independent to validate authored
JSON; this corpus and the numeric override regressions guard that boundary.
"""

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from sil._schema_types import FORMATS
from sil.manifest import Manifest, ManifestError
from sil.recording import read_records
from sil.schema import MessageType

ROOT = Path(__file__).resolve().parents[1]
CORPUS = json.loads((ROOT / "tests/fixtures/schema_conformance.json").read_text())


def test_corpus_covers_every_supported_type():
    assert {f["type"] for c in CORPUS for f in c["fields"]} == set(FORMATS)


def values(case):
    return {f["name"]: bytes(case["values"][f["name"]])
            if f["type"] == "u8" and "count" in f else case["values"][f["name"]]
            for f in case["fields"]}


def literal(value):
    if isinstance(value, list):
        return "{" + ",".join(literal(v) for v in value) + "}"
    if isinstance(value, float):
        return repr(value)
    if value == -9223372036854775808:
        return "(-9223372036854775807LL - 1)"
    return str(value) + ("ULL" if value >= 0 else "LL")


@pytest.mark.parametrize("installed", [False, True], ids=["checkout", "prefix"])
def test_native_codec_kernel_recording_conformance(installed, build_dir, tmp_path, run_sil):
    if installed:
        prefix = tmp_path / "prefix"
        subprocess.run(["cmake", "--install", str(build_dir), "--prefix", str(prefix)],
                       check=True, capture_output=True)
        generator = prefix / "bin/silschema"
        includes = prefix / "include"
        assert (prefix / "bin/_sil_schema_types.py").read_bytes() == (
            ROOT / "python/src/sil/_schema_types.py").read_bytes()
    else:
        generator = ROOT / "tools/silschema.py"
        includes = ROOT / "include"
    schemas = {c["name"]: {"fields": c["fields"]} for c in CORPUS}
    source_schema = tmp_path / "schemas.json"
    source_schema.write_text(json.dumps(schemas))
    # Isolated Python with site disabled proves no package/runtime dependency.
    subprocess.run([sys.executable, "-I", "-S", str(generator), str(source_schema),
                    str(tmp_path / "messages.h")], cwd=tmp_path, check=True)
    declarations, assertions, writes, publishes = [], [], [], []
    expected = b""
    for case in CORPUS:
        name = case["name"]
        payload = bytes.fromhex(case["hex"])
        codec = MessageType(name, schemas[name])
        assert codec.size == len(payload)
        assert codec.pack(**values(case)) == payload
        assert codec.pack(**codec.unpack(payload)) == payload
        expected += payload
        initializer = ",".join(literal(case["values"][f["name"]]) for f in case["fields"])
        declarations.append(f"static {name} value_{name} = {{{initializer}}};")
        assertions.append(f"static_assert(sizeof({name}) == {len(payload)});")
        for field, offset in zip(case["fields"], case["offsets"]):
            assertions.append(f"static_assert(offsetof({name}, {field['name']}) == {offset});")
        writes.append(f"fwrite(&value_{name}, 1, sizeof(value_{name}), stdout);")
        publishes.append(f'if (api->publish(api->ctx, "{name}", &value_{name}, sizeof(value_{name})) != SIL_OK) api->fail(api->ctx, "publish failed");')
    source = tmp_path / "participant.cpp"
    source.write_text('\n'.join([
        '#include "messages.h"', '#include <sil/participant.h>', '#include <cstdio>',
        *declarations, *assertions,
        'static void step(void *user, uint64_t) {',
        'auto api = static_cast<const sil_api_v1 *>(user);', *publishes, '}',
        'extern "C" int sil_participant_init(const sil_api_v1 *api, const char *, const char *) {',
        'return api->register_task(api->ctx, "publish", 1, 0, 0, step, const_cast<sil_api_v1 *>(api));', '}',
        'int main() {', *writes, '}',
    ]))
    executable = tmp_path / "payloads"
    library = tmp_path / "participant.silp"
    for output, flags in [(executable, []), (library, ["-shared", "-fPIC"])]:
        subprocess.run(["c++", "-std=c++20", "-I", str(includes), str(source),
                        *flags, "-o", str(output)], check=True, capture_output=True)
    assert subprocess.check_output([str(executable)]) == expected
    manifest = Manifest(duration_ns=1)
    manifest.add_schemas(schemas)
    for name in schemas:
        manifest.add_channel(name, schema=name)
    manifest.add_native("publisher", library=str(library), publishes=list(schemas))
    path = manifest.write(tmp_path / "manifest.json").path
    result = run_sil(path)
    assert result.returncode == 0, result.stderr
    records = list(read_records(result.mcap_path))
    assert len(records) == len(CORPUS)
    assert {channel: payload.hex() for channel, _, payload in records} == {
        c["name"]: c["hex"] for c in CORPUS}
    assert all(t == 0 for _, t, _ in records)


@pytest.mark.parametrize("field", [
    {"type": "bool"}, {"type": 1}, {"type": "u8", "count": 0},
    {"type": "u8", "count": -1}, {"type": "u8", "count": True},
    {"type": "u8", "count": 1.5}, {"type": "u8", "count": "2"},
    {"type": "u64", "count": 2**63},
])
def test_malformed_schema_rejected_independently(field, tmp_path, run_sil):
    schemas = {"Bad": {"fields": [{"name": "v", **field}]}}
    with pytest.raises(ManifestError):
        Manifest(duration_ns=1).add_schemas(schemas)
    # Bypass the builder completely, including its current-version defaults.
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"sil_manifest": 1, "duration_ns": 1, "schemas": schemas,
                               "channels": {}, "participants": {}}))
    result = run_sil(path)
    assert result.returncode == 2
    assert "schema" in result.stderr.lower(), result.stderr


@pytest.mark.parametrize("case", CORPUS[:8], ids=lambda c: c["name"])
def test_integer_boundaries_reject_outside_values(case):
    field_type = case["fields"][0]["type"]
    codec = MessageType("Scalar", {"fields": [{"name": "v", "type": field_type}]})
    low, high = min(case["values"]["v"]), max(case["values"]["v"])
    for value in (low, high):
        assert codec.unpack(codec.pack(v=value)) == {"v": value}
    for value in (low - 1, high + 1):
        with pytest.raises(struct.error):
            codec.pack(v=value)
