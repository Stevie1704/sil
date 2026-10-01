"""Prepare the #226/#227 experiments for the installed offline runtime."""
from __future__ import annotations

import argparse
import _ctypes
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'proofs/adas-equivalence'))
import experiment
from sil.csv_recording import convert

replay = experiment.load('matrix_replay_proof', ROOT / 'proofs/adas-equivalence/prove.py')
sys.path.insert(0, str(ROOT / 'proofs/adas-closed-loop'))
import loop
closed = loop.load('matrix_closed_proof', ROOT / 'proofs/adas-closed-loop/prove.py')
bundles = experiment.load('matrix_bundle_prepare', ROOT / 'examples/bundle/prepare.py')


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + '\n')


def dependencies(targets: list[Path]) -> tuple[str, ...]:
    """Declare loader dependencies, including the plant's embedded CPython."""
    files = set(bundles.native_libraries(Path(sys.executable)))
    files.update(bundles.native_libraries(Path(_ctypes.__file__)))
    for target in targets:
        if target.suffix != '.fmu':
            files.update(bundles.native_libraries(target))
            continue
        with tempfile.TemporaryDirectory() as temporary:
            with zipfile.ZipFile(target) as archive:
                for name in archive.namelist():
                    if name.startswith('binaries/x86_64-linux/') and name.endswith('.so'):
                        binary = Path(temporary) / Path(name).name
                        binary.write_bytes(archive.read(name))
                        files.update(bundles.native_libraries(binary))
    # PythonFMU3 dlopens CPython; ldd of the FMU does not see that dependency.
    files.update(str(p) for p in Path('/usr/local/lib').glob('libpython3.13.so*')
                 if p.is_file())
    return tuple(sorted(files))


def rewrite(value, paths: dict[str, str]):
    """Relocate explicit Manifest paths; preserve all experiment values."""
    if isinstance(value, dict):
        return {k: rewrite(v, paths) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite(v, paths) for v in value]
    return paths.get(value, value) if isinstance(value, str) else value


def role(path: Path) -> str:
    if path.suffix == '.mcap':
        return 'reference' if 'expected' in path.name or 'fmpy' in path.name else 'recording'
    if '.receipt.' in path.name:
        return 'receipt'
    if '.contract.' in path.name:
        return 'contract'
    return 'conversion-input'


def prepare(out: Path, plant: Path, fmpy_python: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    work = out / 'preparation'
    work.mkdir()
    r = replay.Proof(work / 'replay', fmpy_python, 'cc')
    identities = r.artifacts()
    if identities['matches_pin'] is not True:
        raise RuntimeError('controller archive does not match the qualified compiler pin')
    independent = r.independent()
    if not all(v['passed'] for v in independent.values()):
        raise RuntimeError(f'replay independent preparation failed: {independent}')
    c = closed.Proof(work / 'closed', plant, fmpy_python, 'cc')
    c.work.mkdir(parents=True)
    c.fmu, c.library = r.fmu, r.library
    plant_identity = closed.plant_record(plant)
    if not plant_identity['passed']:
        raise RuntimeError(f'plant identity/interface mismatch: {plant_identity}')
    independent_loop = c.independent()
    if not all(v['passed'] for v in independent_loop.values()):
        raise RuntimeError(f'closed-loop independent preparation failed: {independent_loop}')
    write(work / 'identities.json', {'controller': identities, 'plant': plant_identity})
    write(work / 'independent.json', {'replay': independent, 'closed': independent_loop})
    runtime = bundles.Runtime(Path('/opt/sil/python/bin'), Path('/opt/sil/native/bin'))
    expected = {}

    def build(name: str, kind: str, form: str, control: str | None = None):
        b = bundles.Bundle(Path('/bundles') / name)
        paths = {}
        targets = [r.library, r.fmu, plant]
        for source in targets:
            paths[str(source.resolve())] = str(b.copy(source, 'target'))
        edge = b.copy(loop.EDGE, 'participant')
        paths[str(loop.EDGE)] = str(edge)
        b.copy(ROOT / 'examples/adas-reference/schemas.json', 'resource')
        b.copy(ROOT / 'docs/adas-reference.md', 'resource', 'profile.md')
        b.copy(work / 'identities.json', 'receipt')
        b.copy(work / 'independent.json', 'receipt')
        runs = []
        if kind == 'replay':
            for source in sorted(r.inputs.iterdir()):
                paths[str(source.resolve())] = str(b.copy(source, role(source)))
            for source in sorted((ROOT / 'examples/adas-reference/maneuvers').glob('*.csv')):
                b.copy(source, 'conversion-input', 'authored-' + source.name)
            for case in experiment.CASES.values():
                m = (experiment.native_manifest(case, r.inputs, r.library) if form == 'native'
                     else experiment.fmu_manifest(case, r.inputs, r.fmu))
                # Paths inside the replay command name individual Recordings.
                doc = rewrite(m.to_doc(), paths)
                ref = b.copy(r.fmpy(case), 'reference')
                rows = b.copy(r.work / f'{case.name}.fmpy.csv', 'conversion-input')
                b.copy(r.work / f'{case.name}.case.json', 'conversion-input')
                b.copy(r.work / f'{case.name}.superseded.json', 'receipt')
                receipt = convert(b.root / f'{case.maneuver}.expected.mapping.json', rows, ref)
                write(b.path(f'{case.name}.fmpy.receipt.json', 'receipt'), receipt)
                name_contract = f'{case.maneuver}.contract.json'
                runs.append({'name': case.name, 'manifest': f'{case.name}.json',
                             'determinism': True, 'participant_timeout_ms': 30000,
                             'comparisons': [
                                 {'name': 'oracle', 'contract': name_contract,
                                  'reference': f'{case.name}.expected.mcap'},
                                 {'name': 'independent', 'contract': name_contract,
                                  'reference': ref.name}]})
                write(b.path(f'{case.name}.json', 'manifest'), doc)
        else:
            cases = (loop.ALL_CASES.values() if control is None else
                     [loop.CASES['sensor_loss' if control == 'comparison' else 'approach']])
            for case in cases:
                controller = loop.native_controller(r.library) if form == 'native' else loop.fmu_controller(r.fmu)
                if control in ('manifest', 'malformed'):
                    failure = loop.FAILURES['manifest_error' if control == 'manifest' else 'malformed_list']
                    doc = loop.failure_document(failure, controller, c.plant())
                else:
                    doc = loop.loop_manifest(case, controller, c.plant())
                if control in ('hang', 'crash'):
                    target = b.copy(work / f'{control}.so', 'target')
                    doc['participants']['controller']['library'] = str(target)
                    targets.append(target)
                if control == 'deadline':
                    doc['participants']['controller']['command'][3] = str(b.copy(work / 'hang-fmu/AdasReference.fmu', 'target', 'hang.fmu'))
                doc = rewrite(doc, paths)
                write(b.path(f'{case.name}.json', 'manifest'), doc)
                contract = b.path('independent.contract.json', 'contract')
                write(contract, loop.INDEPENDENT_CONTRACT)
                ref = b.copy(c.fmpy(loop.UNFAULTED if control == 'comparison' else case.name),
                             'reference')
                # Archive the independent input declaration, mapping and source rows.
                reference_name = loop.UNFAULTED if control == 'comparison' else case.name
                for source in (c.work / f'{reference_name}.case.json',
                               c.work / f'{reference_name}.fmpy.csv',
                               c.work / 'independent.mapping.json'):
                    if source.name not in b.artifacts:
                        b.copy(source, 'conversion-input')
                receipt = convert(b.root / 'independent.mapping.json',
                                  b.root / f'{reference_name}.fmpy.csv', ref)
                write(b.path(f'{reference_name}.fmpy.receipt.json', 'receipt'), receipt)
                runs.append({'name': case.name, 'manifest': f'{case.name}.json',
                             'determinism': True,
                             'participant_timeout_ms': 30000,
                             'comparisons': [{'name': 'independent', 'contract': contract.name,
                                              'reference': ref.name}]})
        write(b.path('experiment.json', 'resource'), {
            'profile': experiment.manifest.PROFILE,
            'profile_version': experiment.manifest.PROFILE_VERSION,
            'schema_identity': experiment.sha256(ROOT / 'examples/adas-reference/schemas.json'),
            'schema_version': 3, 'kind': kind, 'form': form,
            'calibration': experiment.manifest.PARAMETERS,
            'freshness_ns': {'radar': 40_000_000, 'camera': 80_000_000, 'ego': 20_000_000},
            'replay_window': 'complete authored duration; warm-up 0; no rebasing',
            'control': control})
        declaration = b.declare(runtime, name, runs, dependencies(targets))
        doc = json.loads(declaration.read_text())
        doc['dependencies']['python']['modules'] += ['lz4', 'zstandard']
        doc['runtime']['environment']['TMPDIR'] = f'/work/tmp/{name}'
        write(declaration, doc)
        expected[name] = {'status': 'pass' if control is None or control == 'tampered' else {
            'manifest': 'manifest-error', 'malformed': 'behavioral-failure',
            'comparison': 'behavioral-failure', 'hang': 'timeout',
            'crash': 'behavioral-failure', 'deadline': 'behavioral-failure'}[control],
                         'timeout_s': 30 if control == 'hang' else 600}

    # Fault libraries are separate targets compiled against the installed ABI.
    source = work / 'fault.c'
    source.write_text('''#include <sil/participant.h>
#include <stdlib.h>
#include <stdio.h>
static void fault(void *u, uint64_t t) {
  (void)u; (void)t;
  char path[4096];
  snprintf(path, sizeof path, "%s/native-callback-entered", getenv("TMPDIR"));
  FILE *marker = fopen(path, "w");
  if (marker) {
    fputs("native fault callback entered\\n", marker);
    fclose(marker);
  }
#ifdef CRASH
  abort();
#else
  for (;;) {}
#endif
}
int sil_participant_init(const sil_api_v1 *a, const char *n, const char *c) {
  (void)n; (void)c;
  return a->register_task(a->ctx, "fault", 10000000, 0, 0, fault, NULL);
}
''')
    for fault in ('hang', 'crash'):
        subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-I/opt/sil/native/include',
                        *(['-DCRASH'] if fault == 'crash' else []), str(source),
                        '-o', str(work / f'{fault}.so')], check=True)
    # Real FMI DoStep stall, so the installed Importer's response deadline is exercised.
    original = replay.package.SOURCES['fmi3_controller.c']
    stalled = work / 'fmi3_controller.c'
    text = original.read_text()
    start = text.index('fmi3Status fmi3DoStep(')
    brace = text.index('{', start)
    stalled.write_text(text[:brace + 1] + '\n  for (;;) {}\n' + text[brace + 1:])
    replay.package.SOURCES['fmi3_controller.c'] = stalled
    replay.package.build(work / 'hang-fmu')
    replay.package.SOURCES['fmi3_controller.c'] = original
    for kind in ('replay', 'closed'):
        for form in ('native', 'fmu'):
            build(f'{kind}-{form}', kind, form)
    for control in ('manifest', 'malformed', 'comparison', 'hang', 'crash', 'deadline', 'tampered'):
        form = 'native' if control in ('hang', 'crash') else 'fmu'
        build(f'control-{control}', 'closed', form, control)
    expected['control-tampered']['status'] = 'manifest-error'
    write(out / 'expected.json', expected)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('out', type=Path)
    p.add_argument('--plant', type=Path, required=True)
    p.add_argument('--fmpy-python', required=True)
    a = p.parse_args()
    prepare(a.out.resolve(), a.plant.resolve(), a.fmpy_python)
