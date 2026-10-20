"""`sil profile --collector heap` with the real Heaptrack (#317).

Opt-in: it runs only with SIL_PROFILE_HEAP set, on Linux with Heaptrack, cc
and objcopy on PATH. The profile-heap-integration workflow runs it on
ubuntu-24.04 (x86-64, Heaptrack 1.5), the qualified machine class. Heaptrack
needs no privilege, so the job changes no host setting.

The workload is the one of the #314 proof (proofs/profiling/workload.py): a
stripped Native library with a separate debug file and known allocation
sites (1600 x 1024 bytes, and one 65536-byte block that is never freed), a
shimmed Process participant (800 x 2048 bytes), and an FMU that the
importer extracts into the Run working directory and loads at run time
(400 x 4096 bytes). The checks compare exact call counts and sizes of
the known sites; no check uses a time, an RSS value or an overhead.
SIL_PROFILE_EVIDENCE names a directory that keeps the profile directories
for upload.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT
from sil.profile_report import validate_profile_report

pytestmark = pytest.mark.skipif(
    not os.environ.get("SIL_PROFILE_HEAP") or platform.system() != "Linux",
    reason="opt-in real Heaptrack check: set SIL_PROFILE_HEAP on Linux")

NATIVE = ("sil-run", "sil-run-diagnostics", "sil-diagnostics-launcher")
NATIVE_SITES = ("silproof_native_alloc_site", "silproof_native_retain_site")
OTHER_SITES = ("silproof_process_alloc_site", "silproof_fmu_alloc_site")


@pytest.fixture(scope="module")
def workload():
    name = "silproof_heap_workload"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "proofs" / "profiling" / "workload.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def built(workload, tmp_path_factory):
    return workload.build(tmp_path_factory.mktemp("workload"))


def tools_in(directory: Path, build_dir: Path) -> Path:
    tools = directory / "tools"
    tools.mkdir()
    for name in NATIVE:
        (tools / name).symlink_to(build_dir / name)
    return tools


def profile_run(tools: Path, cwd: Path, manifest: Path, out: str,
                *scope: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items()
           if key != "LD_PRELOAD"}
    env["PATH"] = os.pathsep.join([str(tools), "/usr/bin:/bin"])
    return subprocess.run(
        [sys.executable, "-m", "sil.cli", "profile", str(manifest), "--out",
         out, "--collector", "heap", *scope, "--participant-timeout-ms",
         "60000"], cwd=cwd, env=env, capture_output=True, text=True,
        timeout=1800)


def keep(directory: Path, name: str) -> None:
    evidence = os.environ.get("SIL_PROFILE_EVIDENCE")
    if evidence and directory.exists():
        target = Path(evidence) / f"heap-{name}"
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(directory, target, symlinks=True)


def with_debug_beside(built, debug: Path) -> None:
    shutil.copy2(debug, built.native.parent / built.native_debug.name)


def capture(build_dir, workload, built, cwd: Path, name: str, *scope):
    tools = tools_in(cwd, build_dir)
    manifest = workload.manifest(built, python=sys.executable).write(
        cwd / "manifest.json").path
    proc = profile_run(tools, cwd, manifest, "prof", *scope)
    keep(cwd / "prof", name)
    return proc


def report_of(cwd: Path) -> dict:
    report = json.loads((cwd / "prof" / "profile.json").read_text())
    assert validate_profile_report(report) == []
    return report


def inclusive(report: dict) -> dict[str, dict]:
    return {row["symbol"]: row
            for row in report["capture"]["analysis"]["hotspots"]["inclusive"]
            if row["symbol"]}


def allocations_by_function(cwd: Path) -> dict[str, tuple[int, set]]:
    """Calls and sizes per function anywhere in the stack, from the
    complete allocations file (not only the kept rows)."""
    document = json.loads(
        (cwd / "prof" / "heap" / "allocations.json").read_text())
    found: dict[str, list] = {}
    for stack, size, calls, _temporary, _outstanding in \
            document["allocations"]:
        names = {document["frames"][f]["symbol"]
                 for f in document["stacks"][stack]}
        for name in names - {None}:
            entry = found.setdefault(name, [0, set()])
            entry[0] += calls
            if calls:
                entry[1].add(size)
    return {name: (calls, sizes) for name, (calls, sizes) in found.items()}


def ordinary_recording(build_dir: Path, cwd: Path) -> bytes:
    done = subprocess.run(
        [str(build_dir / "sil-run"), str(cwd / "manifest.json"), "-o",
         str(cwd / "ordinary.mcap"), "--participant-timeout-ms", "60000"],
        cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return (cwd / "ordinary.mcap").read_bytes()


@pytest.fixture(scope="module")
def runner_scope(workload, built, build_dir, tmp_path_factory):
    cwd = tmp_path_factory.mktemp("runner")
    with_debug_beside(built, built.native_debug)
    return cwd, capture(build_dir, workload, built, cwd, "runner",
                        "--runner")


@pytest.fixture(scope="module")
def fmu_scope(workload, built, build_dir, tmp_path_factory):
    cwd = tmp_path_factory.mktemp("fmu")
    with_debug_beside(built, built.native_debug)
    return cwd, capture(build_dir, workload, built, cwd, "fmu",
                        "--participant", "fmu")


def test_the_runner_capture_is_complete(runner_scope):
    cwd, proc = runner_scope

    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = report_of(cwd)
    capture = report["capture"]
    assert (capture["status"], capture["scope"]) == ("complete",
                                                     {"kind": "runner"})
    assert capture["backend"]["version"].startswith("heaptrack")
    assert "sil-run-diagnostics" in capture["analysis"]["traced_executable"]
    assert capture["activation_correlation"]["status"] == "unavailable"
    assert not list(cwd.glob(".sil-run-*"))


def test_the_native_sites_have_their_exact_calls_and_sizes(runner_scope,
                                                           workload):
    cwd, _ = runner_scope
    found = allocations_by_function(cwd)

    for site in NATIVE_SITES:
        calls, size = workload.ALLOCATION_SITES[site]
        assert found[site] == (calls, {size}), (site, found.get(site))


def interpreter_stats(cwd: Path) -> dict[str, int]:
    """The totals that heaptrack_interpret itself prints at its end."""
    text = (cwd / "prof" / "heap" / "interpret.stderr.txt").read_text()
    return {name: int(re.search(rf"^\s*{label}:\s*(\d+)", text,
                                re.MULTILINE)[1])
            for name, label in (("allocations", "allocations"),
                                ("outstanding_allocations",
                                 "leaked allocations"),
                                ("temporary_allocations",
                                 "temporary allocations"))}


def print_stats(cwd: Path) -> dict[str, int] | None:
    """Calls and temporary allocations as heaptrack_print counts them."""
    printer = shutil.which("heaptrack_print")
    if printer is None:
        return None
    done = subprocess.run([printer, str(cwd / "prof" / "heap" /
                                        "heaptrack.data")],
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr
    return {name: int(re.search(rf"^{label}: (\d+)", done.stdout,
                                re.MULTILINE)[1])
            for name, label in (("allocations",
                                 "calls to allocation functions"),
                                ("temporary_allocations",
                                 "temporary memory allocations"))}


@pytest.mark.parametrize("scope", ["runner_scope", "fmu_scope"])
def test_the_totals_agree_with_heaptracks_own_counts(scope, request):
    cwd, _ = request.getfixturevalue(scope)
    analysis = report_of(cwd)["capture"]["analysis"]
    totals = analysis["totals"]
    interpreted = interpreter_stats(cwd)
    printed = print_stats(cwd)

    assert totals["allocations"] == interpreted["allocations"]
    assert totals["outstanding_allocations"] == \
        interpreted["outstanding_allocations"]
    # Temporary: the interpreter compares addresses, so it counts at most
    # what the data file shows; heaptrack_print also counts a free of info
    # 0 after any free, so it counts at least as many.
    assert interpreted["temporary_allocations"] <= \
        totals["temporary_allocations"]
    if printed is not None:
        assert printed["allocations"] == totals["allocations"]
        assert totals["temporary_allocations"] <= \
            printed["temporary_allocations"]
    assert analysis["coverage"]["undecodable_records"] == 0


def test_the_native_library_keeps_its_path_and_build_id(runner_scope,
                                                        built):
    cwd, _ = runner_scope
    modules = report_of(cwd)["capture"]["analysis"]["modules"]

    native, = [m for m in modules if m["path"] == str(built.native)]
    assert native["build_id"]
    assert native["allocations_leaf"] > 0
    assert native["allocations_unknown_symbol"] == 0


def test_the_retained_block_is_outstanding_at_capture_end(runner_scope):
    cwd, _ = runner_scope
    rows = inclusive(report_of(cwd))

    retained = rows["silproof_native_retain_site"]
    assert (retained["outstanding_allocations"],
            retained["outstanding_bytes"]) == (1, 65536)
    assert rows["silproof_native_alloc_site"]["outstanding_allocations"] == 0


def test_the_runner_capture_holds_no_process_participant_allocation(
        runner_scope):
    cwd, _ = runner_scope
    found = allocations_by_function(cwd)

    for site in OTHER_SITES:
        assert site not in found


def test_the_runner_recording_equals_the_ordinary_one(runner_scope,
                                                      build_dir):
    cwd, _ = runner_scope

    assert (cwd / "prof" / "run.mcap").read_bytes() == \
        ordinary_recording(build_dir, cwd)


def test_heaptrack_print_reads_the_raw_data(runner_scope):
    cwd, _ = runner_scope
    printer = shutil.which("heaptrack_print")
    if printer is None:
        pytest.skip("heaptrack_print is not installed")

    done = subprocess.run([printer, str(cwd / "prof" / "heap" /
                                        "heaptrack.data")],
                          capture_output=True, text=True, timeout=600)

    assert done.returncode == 0, done.stderr
    assert "silproof_native_alloc_site" in done.stdout


def test_the_fmu_scope_holds_the_fmu_allocations_only(fmu_scope, workload):
    cwd, proc = fmu_scope

    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = report_of(cwd)
    capture = report["capture"]
    assert capture["status"] == "complete"
    assert capture["scope"] == {"kind": "process_participant",
                                "participant": "fmu"}
    assert "sil.fmi" in capture["analysis"]["traced_executable"]
    found = allocations_by_function(cwd)
    calls, size = workload.ALLOCATION_SITES["silproof_fmu_alloc_site"]
    assert found["silproof_fmu_alloc_site"] == (calls, {size})
    for site in (*NATIVE_SITES, "silproof_process_alloc_site"):
        assert site not in found


def test_the_removed_fmu_library_is_named_and_its_identity_is_unknown(
        fmu_scope):
    # The importer loads the FMU from the Run working directory and removes
    # it itself at shutdown. The live interpreter named its functions while
    # the file existed (the site test above); Heaptrack records no build
    # ID, and no file is left to read one from.
    cwd, _ = fmu_scope
    modules = report_of(cwd)["capture"]["analysis"]["modules"]

    fmu = [m for m in modules if m["path"].endswith("EgoMotion.so")]
    assert fmu and all(m["allocations_leaf"] for m in fmu)
    assert all(m["build_id"] is None and m["unavailable"] == {
        "build_id": "module_unavailable"} for m in fmu)
    assert not list(cwd.glob(".sil-run-*"))


def test_the_fmu_recording_equals_the_ordinary_one(fmu_scope, build_dir):
    cwd, _ = fmu_scope

    assert (cwd / "prof" / "run.mcap").read_bytes() == \
        ordinary_recording(build_dir, cwd)


def test_a_mismatched_debug_file_names_no_stripped_site(
        workload, built, build_dir, runner_scope, tmp_path):
    with_debug_beside(built, built.native_mismatched_debug)
    try:
        proc = capture(build_dir, workload, built, tmp_path, "mismatched",
                       "--runner")
    finally:
        with_debug_beside(built, built.native_debug)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    found = allocations_by_function(tmp_path)
    assert "silproof_native_alloc_site" not in found
    # The allocations stay in the evidence, without a name.
    document = json.loads(
        (tmp_path / "prof" / "heap" / "allocations.json").read_text())
    assert sum(calls for _, size, calls, _, _ in document["allocations"]
               if size == 1024) >= workload.ALLOCATION_SITES[
                   "silproof_native_alloc_site"][0]


@pytest.mark.parametrize("participant, message", [
    ("process", "uses the Clock shim"),
    ("native", "is a Native participant"),
])
def test_unsupported_scopes_stop_before_the_run(
        workload, built, build_dir, tmp_path, participant, message):
    proc = capture(build_dir, workload, built, tmp_path, participant,
                   "--participant", participant)

    assert proc.returncode == 2, proc.stderr
    assert message in proc.stderr
    assert not (tmp_path / "prof").exists()


# A custom pool in a Native participant: silarena_create takes one block of
# ARENA_BYTES from malloc at init; each Activation then takes ARENA_OBJECTS
# objects from it with silarena_take, which calls no allocation function.
ARENA_OBJECTS, ARENA_OBJECT_BYTES = 16, 64
ARENA_SOURCE = f"""
#include <stdlib.h>
#include <string.h>
#include <sil/participant.h>

#define OBJECTS {ARENA_OBJECTS}
#define OBJECT_BYTES {ARENA_OBJECT_BYTES}
#define ARENA_BYTES (OBJECTS * OBJECT_BYTES)

typedef struct {{ unsigned char *arena; size_t used; }} State;
static State g_state;
volatile unsigned char g_sink;

__attribute__((noinline, used)) unsigned char *silarena_create(size_t n) {{
  return malloc(n);
}}

__attribute__((noinline, used)) void *silarena_take(State *s, size_t n) {{
  if (s->used + n > ARENA_BYTES) return NULL;
  void *object = s->arena + s->used;
  s->used += n;
  return object;
}}

static void arena_task(void *user, uint64_t now_ns) {{
  (void)now_ns;
  State *s = (State *)user;
  s->used = 0;
  for (int i = 0; i < OBJECTS; i++) {{
    unsigned char *object = silarena_take(s, OBJECT_BYTES);
    if (object) {{ memset(object, i, OBJECT_BYTES); g_sink = object[0]; }}
  }}
}}

int sil_participant_init(const sil_api_v1 *api, const char *name,
                         const char *config_json) {{
  (void)name;
  (void)config_json;
  if (api->abi_version != SIL_ABI_VERSION) return SIL_ERR;
  g_state.arena = silarena_create(ARENA_BYTES);
  if (!g_state.arena) return SIL_ERR;
  g_state.used = 0;
  return api->register_task(api->ctx, "arena", 10000000ULL, 0, 0,
                            arena_task, &g_state);
}}
"""


def test_a_custom_pool_shows_only_its_backing_allocation(build_dir,
                                                         tmp_path):
    from sil.manifest import Manifest

    source = tmp_path / "silarena.c"
    source.write_text(ARENA_SOURCE)
    library = tmp_path / "libsilarena.so"
    subprocess.run(["cc", "-O0", "-g", "-shared", "-fPIC",
                    f"-I{ROOT / 'include'}", "-o", str(library),
                    str(source)], check=True)
    m = Manifest(duration_ns=100_000_000)
    m.add_native("arena", library=str(library))
    manifest = m.write(tmp_path / "manifest.json").path
    tools = tools_in(tmp_path, build_dir)

    proc = profile_run(tools, tmp_path, manifest, "prof", "--runner")
    keep(tmp_path / "prof", "arena")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    found = allocations_by_function(tmp_path)
    assert found["silarena_create"] == (
        1, {ARENA_OBJECTS * ARENA_OBJECT_BYTES})
    # 10 Activations took 160 objects from the arena; none is counted.
    assert "silarena_take" not in found
    assert "arena_task" not in found
