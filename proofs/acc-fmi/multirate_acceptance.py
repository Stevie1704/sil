"""Run pinned multi-rate coupling from an installed Linux SiL image."""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import shutil
import platform
import sys
from dataclasses import asdict
from pathlib import Path

from sil.fmi.coupling import couple
from multirate_compare import (compare_independent, first_difference,
                               recording, sensitivity, trajectory)
from multirate_contract import (ENVELOPE, FAULT_ROW, FAULTS, FIELDS, FMU_DIR,
                                GRID_NS, INPUT, MODELS, ROW, ROWS, coupling,
                                declaration, delivery_schedule, pinned_schedule,
                                varied)
from proof_support import (compare_files, file_sha256, require, run_logged,
                           sil_runner_args, write_json)


def require_installed_image():
    require(platform.system() == 'Linux' and platform.machine() == 'x86_64',
            'multi-rate evidence requires installed Linux x86-64')
    import sil
    require(Path(sil.__file__).resolve().parent.parent.name == 'site-packages',
            'SiL must be imported from an installed distribution')
    for module in ('fmpy', 'pythonfmu3', 'pytest'):
        require(importlib.util.find_spec(module) is None,
                f'{module} must not be installed in the example image')
    for tool in ('cmake', 'gcc', 'git'):
        require(shutil.which(tool) is None, f'{tool} must not be in the example image')
    return sil


def verify(bundle: Path):
    index = json.loads((bundle / 'bundle.json').read_text())
    require(index['format'] == 2, 'unexpected multi-rate bundle format')
    actual = {str(p.relative_to(bundle)): file_sha256(p)
              for p in sorted(bundle.rglob('*')) if p.is_file() and p.name != 'bundle.json'}
    require(actual == index['files'], 'multi-rate bundle contents differ from pinned digests')
    contract = json.loads((bundle / 'contract.json').read_text())
    require(contract == json.loads(json.dumps(declaration())),
            'installed experiment differs from pinned contract')
    for model, identity in index['models'].items():
        require(file_sha256(FMU_DIR / f'{model}.fmu') == identity['sha256'],
                f'{model}: installed archive differs from pinned archive')
    images = json.loads((bundle / 'images.json').read_text())
    require(images['platform'] == 'linux/amd64' and
            images['example_id'] == os.environ.get('SIL_ACC_EXAMPLE_IMAGE_ID'),
            'the installed example image differs from the pinned bundle')
    return index


def check_plan_schedule(receipt, row):
    """Cross-check independently authored delivery against the public plan."""
    checked = {}
    for route in receipt['plan']['routes']:
        channel = route['channel']
        expected = delivery_schedule(row, channel)
        for activation in route['activations']:
            at = activation['at_ns']
            publication = expected[at]
            taken = activation['input']
            actual = None if taken == 'start' else taken['published_ns']
            require(actual == publication,
                    f"{row.name} {channel} at {at}: plan takes {actual}, "
                    f"independent schedule expects {publication}")
        checked[channel] = len(route['activations'])
    require(set(checked) == set(INPUT.values()),
            f'{row.name}: plan route coverage differs')
    return checked


def _independent(bundle, name):
    return json.loads((bundle / 'independent' / f'{name}.json').read_text())


def run_row(bundle: Path, row, out: Path):
    """Author and run one Row twice; compare it with its independent run."""
    authored = bundle / f'{row.name}.coupling.json'
    require(json.loads(authored.read_text()) == coupling(row),
            f'{row.name}: authored coupling differs from pinned row')
    schedule = json.loads((bundle / 'independent' / f'{row.name}.schedule.json').read_text())
    require(schedule == pinned_schedule(row),
            f'{row.name}: independently authored input schedule differs')
    archives: dict[str, str | Path] = {name: FMU_DIR / f'{model}.fmu'
                                       for name, model in MODELS.items()}
    manifest = out / f'{row.name}.json'
    receipt = couple(authored, archives, manifest)
    schedule_checked = check_plan_schedule(receipt, row)
    again = out / f'{row.name}-again.json'
    couple(authored, archives, again)
    manifest_sha = compare_files(manifest, again)
    recordings = [out / f'{row.name}-{n}.mcap' for n in (1, 2)]
    for path in recordings:
        run_logged(sil_runner_args(manifest, path), path.with_suffix('.log'))
    recording_sha = compare_files(*recordings)
    actual = recording(recordings[0], row)
    comparison = compare_independent(actual, _independent(bundle, row.name), row)
    return actual, {
        'row': asdict(row), 'manifest_sha256': manifest_sha,
        'recording_sha256': recording_sha,
        'fmu_sha256': {model: receipt['fmus'][model]['sha256'] for model in archives},
        'independent_comparison': comparison,
        'plan_schedule_checked_activations': schedule_checked,
        'sample_time_offsets_ns': {channel: row.publication_period(channel)
                                   for channel in FIELDS},
        'schedule_first_activations': {channel: list(schedule[channel].items())[:5]
                                       for channel in INPUT.values()}}


def judge_sensitivity(measured):
    """Each Row against the baseline that differs in one Period or Latency."""
    judged = {}
    for row in ROWS:
        if row.baseline is None:
            continue
        result = sensitivity(measured[row.name], row,
                             measured[row.baseline], ROW[row.baseline])
        require(result['inside_envelope'], f'{row.name}: outside declared envelope: {result}')
        judged[row.name] = {'baseline': row.baseline, 'varied': varied(row), **result}
    return judged


def judge_faults(bundle: Path, measured):
    """The SiL Recording of FAULT_ROW has to differ from every faulty run."""
    judged = {}
    for fault in FAULTS:
        faulty = trajectory(_independent(bundle, f'fault-{fault}'))
        difference = first_difference(measured[FAULT_ROW.name], faulty, FAULT_ROW)
        require(difference is not None, f'{fault}: negative control was not detected')
        judged[fault] = {'row': FAULT_ROW.name, 'first_difference': difference}
    wrong_start = trajectory(_independent(bundle, 'fault-wrong-initialization'))
    outside = sensitivity(wrong_start, FAULT_ROW, measured[FAULT_ROW.name], FAULT_ROW)
    require(not outside['inside_envelope'],
            'deliberately wrong initialization remained inside envelope')
    judged['wrong-initialization']['sensitivity'] = outside
    return judged


def run(bundle: Path, out: Path):
    sil = require_installed_image()
    index = verify(bundle)
    out.mkdir(parents=True, exist_ok=True)
    results, measured = {}, {}
    for row in ROWS:
        measured[row.name], results[row.name] = run_row(bundle, row, out)
    for name, result in judge_sensitivity(measured).items():
        results[name]['sensitivity'] = result
    report = {'issue': 197, 'platform': platform.platform(),
              'installed_sil': {'version': importlib.metadata.version('sil'),
                                'module': str(Path(sil.__file__).resolve())},
              'bundle_sha256': file_sha256(bundle / 'bundle.json'),
              'images': json.loads((bundle / 'images.json').read_text()),
              'fmus': index['models'], 'common_observation_grid_ns': GRID_NS,
              'sensitivity_envelope': ENVELOPE,
              'scope': 'AccController and AccPlant PythonFMU3 FMI 3.0 Co-Simulation, fixed 10/20 ms periods, stated Channel Latencies, Linux x86-64',
              'results': results,
              'negative_controls': judge_faults(bundle, measured),
              'determinism_policy': 'byte identity only for repeated Runs of each exact Manifest; independent FMPy comparison uses tolerances'}
    write_json(out / 'report.json', report)
    return report


if __name__ == '__main__':
    run(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
