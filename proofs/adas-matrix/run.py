"""Seal once, or run and check the installed ADAS acceptance matrices offline."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BUNDLE_ROOT = Path('/bundles')
EXPECTED = Path('/opt/adas/expected.json')


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + '\n')


def seal() -> None:
    cases = {'nominal': [], 'controls': []}
    for name, expected in json.loads(EXPECTED.read_text()).items():
        root = BUNDLE_ROOT / name
        proc = subprocess.run(['sil-bundle', 'seal', str(root)],
                              capture_output=True, text=True, check=True)
        lock = hashlib.sha256((root / 'bundle.lock.json').read_bytes()).hexdigest()
        print(proc.stdout, end='')
        cases['controls' if name.startswith('control-') else 'nominal'].append({
            'name': name, 'bundle': str(root), 'expect_lock': lock,
            'timeout_s': expected['timeout_s']})
    for kind, entries in cases.items():
        write(Path(f'/opt/adas/{kind}.json'), {'sil_matrix': 1, 'cases': entries})
    # Deliberately alter the sealed bundle, never reseal it.
    with (BUNDLE_ROOT / 'control-tampered/profile.md').open('a') as f:
        f.write('\nDeliberate tampering control.\n')


def parent(pid: int) -> int:
    # The command name in field 2 may contain spaces; it ends at the last ')'.
    stat = Path(f'/proc/{pid}/stat').read_text()
    return int(stat[stat.rindex(')') + 2:].split()[1])


def processes() -> list[int]:
    """Live processes other than this driver and its ancestors; Linux only.

    The consumer container holds nothing else, so any other process leaked
    from a case, whatever its command line names.
    """
    own, pid = set(), os.getpid()
    while pid > 0:
        own.add(pid)
        pid = parent(pid)
    found = []
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit() and int(entry.name) not in own:
            try:
                parent(int(entry.name))
            except (FileNotFoundError, ProcessLookupError):
                continue  # exited while listing
            found.append(int(entry.name))
    return found


def run(out: Path) -> bool:
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise SystemExit('name a new, empty evidence directory')
    expected = json.loads(EXPECTED.read_text())
    for name in expected:
        temporary = Path(f'/work/tmp/{name}')
        temporary.mkdir(parents=True, exist_ok=True)
        if any(temporary.iterdir()):
            raise SystemExit(f'{temporary}: leftover files from an earlier execution')
    checks = {}
    for kind, exit_code in (('nominal', 0), ('controls', 1)):
        proc = subprocess.run(['sil-matrix', f'/opt/adas/{kind}.json', '-o',
                               str(out / kind), '--jobs', '1'],
                              capture_output=True, text=True)
        (out / f'{kind}.log').write_text(proc.stdout + proc.stderr)
        summary = out / kind / 'summary.json'
        # A matrix that wrote no summary fails its case-set check below.
        result = json.loads(summary.read_text()) if summary.is_file() else {'cases': []}
        entries = {c['name']: c for c in result['cases']}
        names = {n for n in expected if n.startswith('control-') == (kind == 'controls')}
        checks[kind] = {'exit_code': proc.returncode,
                        'passed': proc.returncode == exit_code and set(entries) == names,
                        'cases': {}}
        for name in names:
            entry = entries.get(name, {})
            check = {'status': entry.get('status'), 'expected': expected[name]['status'],
                     'passed': entry.get('status') == expected[name]['status']}
            evidence = out / kind / 'cases' / name
            temporary = Path(f'/work/tmp/{name}')
            if name in ('control-hang', 'control-crash'):
                # Control evidence, not residue: record it, then remove it so
                # the forced cleanup below lists only what the Run left behind.
                marker = temporary / 'native-callback-entered'
                check['callback_entered'] = marker.is_file() and marker.read_text() == 'native fault callback entered\n'
                check['passed'] &= check['callback_entered']
                marker.unlink(missing_ok=True)
            leftovers = sorted(evidence.rglob('.sil-run-*'))
            leftovers += sorted(evidence.rglob('core'))
            leftovers += sorted(evidence.rglob('*.core'))
            leftovers += sorted(temporary.iterdir())
            alive = processes()
            check['leftover_processes'] = alive
            check['forced_cleanup'] = []
            # Forced termination cannot run C++ destructors. Only these two controls
            # allow residue, removed by the whole-case owner after all children die.
            if not alive and name in ('control-hang', 'control-crash'):
                for path in leftovers:
                    check['forced_cleanup'].append(str(path))
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                leftovers = []
            check['leftover_files'] = [str(p) for p in leftovers]
            check['passed'] &= not alive and not leftovers
            log = '\n'.join(p.read_text() for p in evidence.rglob('run-1.log'))
            # The same diagnostics that #227 requires for these failures.
            diagnostics = {
                'control-manifest': ["participant 'radar'", 'config keys'],
                'control-malformed': ['radar.count 9 exceeds the capacity 8'],
                'control-deadline': ['timeout waiting for step_done response'],
            }
            if name in diagnostics:
                check['diagnostic'] = diagnostics[name]
                check['passed'] &= all(d in log for d in diagnostics[name])
            bundle_codes = {'control-manifest': 1, 'control-malformed': 1,
                            'control-comparison': 1, 'control-crash': 1,
                            'control-deadline': 1, 'control-tampered': 2}
            if name in bundle_codes:
                check['bundle_exit_code'] = entry.get('bundle_exit_code')
                check['passed'] &= entry.get('bundle_exit_code') == bundle_codes[name]
            if name in ('control-manifest', 'control-malformed', 'control-deadline'):
                code = 2 if name == 'control-manifest' else 1
                check['passed'] &= len(entry.get('runs', [])) == 1 and entry['runs'][0]['exit_code'] == code
            if name == 'control-tampered':
                check['passed'] &= not entry.get('runs')
            if name == 'control-crash':
                check['passed'] &= any(r.get('exit_code') == -6 for r in entry.get('runs', []))
            if name == 'control-hang':
                check['passed'] &= '30 s guard' in entry.get('reason', '')
                check['passed'] &= bool(check['forced_cleanup'])
            if name == 'control-tampered':
                check['passed'] &= 'altered artifact profile.md' in entry.get('reason', '')
            if name == 'control-comparison':
                reports = list(evidence.rglob('independent.comparison.json'))
                first = json.loads(reports[0].read_text()).get('first_divergence') if reports else None
                check['first_divergence'] = first
                predicted = {'kind': 'value', 'observation_ns': 1010000000,
                             'channel': 'adas.command', 'field': 'radar_age_ns',
                             'actual': 20000000, 'expected': 0}
                check['passed'] &= first is not None and all(first.get(k) == v for k, v in predicted.items())
            checks[kind]['cases'][name] = check
        checks[kind]['passed'] &= all(c['passed'] for c in checks[kind]['cases'].values())
    result = {'passed': all(c['passed'] for c in checks.values()), 'matrices': checks}
    write(out / 'acceptance.json', result)
    print(json.dumps(result, indent=2))
    return result['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seal', action='store_true')
    parser.add_argument('out', type=Path, nargs='?', default=Path('/work/evidence'))
    args = parser.parse_args()
    if args.seal:
        seal()
    else:
        sys.exit(0 if run(args.out.resolve()) else 1)
