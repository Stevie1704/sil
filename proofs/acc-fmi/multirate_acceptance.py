"""Run pinned multi-rate coupling from an installed Linux SiL image."""
from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import platform
import sys
from pathlib import Path

from sil.fmi.coupling import couple
from multirate_compare import (Difference, compare_reference, first_difference,
                               recording, sensitivity)
from multirate_contract import (ATOL, CONTROLS, DURATION_NS, ENVELOPE, FIELDS,
                                GRID_NS, ROWS, RTOL, coupling, delivery_schedule)
from proof_support import (compare_files, file_sha256, require, run_logged,
                           sil_runner_args, write_json)


def verify(bundle: Path):
    index = json.loads((bundle / 'bundle.json').read_text())
    require(index['format'] == 1, 'unexpected multi-rate bundle format')
    actual = {str(p.relative_to(bundle)): file_sha256(p)
              for p in sorted(bundle.rglob('*')) if p.is_file() and p.name != 'bundle.json'}
    require(actual == index['files'], 'multi-rate bundle contents differ from pinned digests')
    contract = json.loads((bundle / 'contract.json').read_text())
    require(contract['rows'] == [r.__dict__ for r in ROWS] and
            contract['controls'] == [r.__dict__ for r in CONTROLS] and
            contract['sensitivity_envelope'] == ENVELOPE and
            contract['atol'] == ATOL and contract['rtol'] == RTOL and
            contract['duration_ns'] == DURATION_NS and
            contract['common_grid_ns'] == GRID_NS and
            contract['fields'] == {name: list(fields) for name, fields in FIELDS.items()},
            'installed experiment differs from pinned contract')
    for model, identity in index['models'].items():
        require(file_sha256(Path('/fmus') / f'{model}.fmu') == identity['sha256'],
                f'{model}: installed archive differs from pinned archive')
    images = json.loads((bundle / 'images.json').read_text())
    require(images['platform'] == 'linux/amd64' and
            images['example_id'] == os.environ.get('SIL_ACC_EXAMPLE_IMAGE_ID'),
            'the installed example image differs from the pinned bundle')
    return index


def _reference(bundle, name):
    return json.loads((bundle / 'references' / f'{name}.json').read_text())


def run(bundle: Path, out: Path):
    require(platform.system() == 'Linux' and platform.machine() == 'x86_64',
            'multi-rate evidence requires installed Linux x86-64')
    import sil
    require(Path(sil.__file__).resolve().parent.parent.name == 'site-packages',
            'SiL must be imported from an installed distribution')
    for module in ('fmpy', 'pythonfmu3', 'pytest'):
        require(__import__('importlib.util', fromlist=['find_spec']).find_spec(module) is None,
                f'{module} must not be installed in the run image')
    for tool in ('cmake', 'gcc', 'git'):
        require(shutil.which(tool) is None, f'{tool} must not be in the run image')
    index = verify(bundle)
    out.mkdir(parents=True, exist_ok=True)
    results, measured = {}, {}
    archives: dict[str, str | Path] = {name: Path('/fmus') / f'Acc{name.title()}.fmu'
                for name in ('plant', 'controller')}
    for row in (*ROWS, *CONTROLS):
        name = row.name
        authored = bundle / f'{name}.coupling.json'
        require(json.loads(authored.read_text()) == coupling(row),
                f'{name}: authored coupling differs from pinned row')
        schedule = json.loads((bundle / 'references' / f'{name}.schedule.json').read_text())
        require(schedule == {ch: {str(t): p for t, p in delivery_schedule(row, ch).items()}
                             for ch in ('sensing', 'command')},
                f'{name}: independently authored input schedule differs')
        manifest = out / f'{name}.json'
        receipt = couple(authored, archives, manifest)
        again = out / f'{name}-again.json'
        couple(authored, archives, again)
        manifest_sha = compare_files(manifest, again)
        recordings = [out / f'{name}-{n}.mcap' for n in (1, 2)]
        for path in recordings:
            run_logged(sil_runner_args(manifest, path), path.with_suffix('.log'))
        recording_sha = compare_files(*recordings)
        actual = recording(recordings[0], row)
        comparison = compare_reference(actual, _reference(bundle, name), row)
        measured[name] = actual
        results[name] = {'row': row.__dict__, 'manifest_sha256': manifest_sha,
                         'recording_sha256': recording_sha,
                         'fmu_sha256': {model: receipt['fmus'][model]['sha256']
                                        for model in archives},
                         'independent_comparison': comparison,
                         'observation_offsets_ns': {
                             'sensing': row.plant_ms * 1_000_000,
                             'state': row.plant_ms * 1_000_000,
                             'command': row.controller_ms * 1_000_000},
                         'schedule_first_activations': {
                             ch: list(schedule[ch].items())[:5]
                             for ch in ('sensing', 'command')}}
    baseline = measured['equal-10']
    baseline_row = ROWS[0]
    for row in ROWS:
        result = sensitivity(measured[row.name], row, baseline, baseline_row)
        require(result['inside_envelope'], f'{row.name}: outside declared envelope: {result}')
        results[row.name]['sensitivity'] = result
    zero = next(row for row in ROWS if row.name == 'zero-sensing')
    for row in CONTROLS:
        difference = first_difference(measured[row.name], measured[zero.name], row)
        require(difference is not None, f'{row.name}: negative control was not detected')
        results[row.name]['first_difference_from_zero_sensing'] = difference
    wrong = _reference(bundle, 'wrong-zero-order')
    wrong_values = {channel: {int(t): values for t, values in rows.items()}
                    for channel, rows in wrong['outputs'].items()}
    difference = first_difference(measured[zero.name], wrong_values, zero)
    require(difference is not None, 'wrong zero-Latency order was not detected')
    results['wrong-zero-order'] = {'first_difference': difference,
        'wrong_schedule_first_activations': list(wrong['schedule']['sensing'].items())[:5]}
    outside = sensitivity(measured['wrong-initialization'], CONTROLS[0], baseline, baseline_row)
    require(not outside['inside_envelope'], 'deliberately wrong initialization remained inside envelope')
    results['wrong-initialization']['sensitivity'] = outside
    report = {'issue': 197, 'platform': platform.platform(),
              'installed_sil': {'version': importlib.metadata.version('sil'),
                                'module': str(Path(sil.__file__).resolve())},
              'bundle_sha256': file_sha256(bundle / 'bundle.json'),
              'images': json.loads((bundle / 'images.json').read_text()),
              'fmus': index['models'], 'common_observation_grid_ns': GRID_NS,
              'sensitivity_envelope': ENVELOPE,
              'scope': 'AccController and AccPlant PythonFMU3 FMI 3.0 Co-Simulation, fixed 10/20 ms periods, stated Channel Latencies, Linux x86-64',
              'results': results,
              'determinism_policy': 'byte identity only for repeated Runs of each exact Manifest; independent FMPy comparison uses tolerances'}
    write_json(out / 'report.json', report)
    return report


if __name__ == '__main__':
    run(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
