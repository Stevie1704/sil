"""Linux installed CLI acceptance, against the prepared consumer image.

Run after proofs/adas-matrix/run-proof.sh with
SIL_ADAS_MATRIX_IMAGE=sil-adas-matrix:example pytest tests/test_adas_matrix.py.
The ordinary suite has no Docker/image dependency.
"""
from __future__ import annotations

import json
import os
import subprocess
import pytest


@pytest.fixture(scope='module')
def image():
    name = os.environ.get('SIL_ADAS_MATRIX_IMAGE')
    if not name:
        pytest.skip('set SIL_ADAS_MATRIX_IMAGE to the prepared Linux x86-64 example')
    return name


def consumer(image, tmp_path, optional_controls=False):
    created = subprocess.run(['docker', 'create', '--platform', 'linux/amd64',
                              '--network', 'none', '--init', image],
                             check=True, capture_output=True, text=True)
    container = created.stdout.strip()
    try:
        if optional_controls:
            config = tmp_path / 'controls.json'
            subprocess.run(['docker', 'cp', f'{container}:/opt/adas/controls.json',
                            str(config)], check=True, capture_output=True)
            doc = json.loads(config.read_text())
            for case in doc['cases']:
                case['required'] = False
            config.write_text(json.dumps(doc))
            subprocess.run(['docker', 'cp', str(config),
                            f'{container}:/opt/adas/controls.json'], check=True,
                           capture_output=True)
            # This test isolates control aggregation: keep one nominal case.
            for filename in ('nominal.json', 'expected.json'):
                local = tmp_path / filename
                subprocess.run(['docker', 'cp', f'{container}:/opt/adas/{filename}',
                                str(local)], check=True, capture_output=True)
                content = json.loads(local.read_text())
                if filename == 'nominal.json':
                    content['cases'] = [c for c in content['cases']
                                        if c['name'] == 'replay-native']
                else:
                    content = {n: c for n, c in content.items()
                               if n.startswith('control-') or n == 'replay-native'}
                local.write_text(json.dumps(content))
                subprocess.run(['docker', 'cp', str(local),
                                f'{container}:/opt/adas/{filename}'], check=True,
                               capture_output=True)
        proc = subprocess.run(['docker', 'start', '-a', container],
                              capture_output=True, text=True, timeout=900)
        subprocess.run(['docker', 'cp', f'{container}:/work/evidence', str(tmp_path)],
                       check=True, capture_output=True)
        report = json.loads((tmp_path / 'evidence/acceptance.json').read_text())
        return proc, report, tmp_path / 'evidence'
    finally:
        subprocess.run(['docker', 'rm', '-f', container], check=True,
                       capture_output=True)


def test_clean_installed_matrix_checks_passes_and_selected_failures(image, tmp_path):
    probe = subprocess.run([
        'docker', 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
        '--entrypoint', 'python', image, '-I', '-c',
        "import importlib.util,json,os,pathlib,sil,subprocess; "
        "print(json.dumps({'uid':os.getuid(),'sil':sil.__file__,"
        "'source_tree':pathlib.Path('/src').exists(),"
        "'exporter':importlib.util.find_spec('pythonfmu3') is not None,"
        "'independent_importer':importlib.util.find_spec('fmpy') is not None,"
        "'header':pathlib.Path('/opt/sil/native/include/sil/participant.h').is_file(),"
        "'schema_generator':pathlib.Path('/opt/sil/native/bin/silschema').is_file(),"
        "'runner':subprocess.run(['sil-run','--version'],capture_output=True).returncode,"
        "'generated':subprocess.run(['silschema','/bundles/replay-native/schemas.json',"
        "'/tmp/schemas.h'],capture_output=True).returncode == 0 and "
        "'typedef struct adas_Command' in pathlib.Path('/tmp/schemas.h').read_text(),"
        "'compilers':[str(d/c) for d in map(pathlib.Path,('/usr/bin','/usr/local/bin'))"
        " for c in ('cc','gcc','clang','c++','g++') if (d/c).exists()]}))"
    ], check=True, capture_output=True, text=True)
    installation = json.loads(probe.stdout)
    assert installation['uid'] == 10001
    assert installation['sil'].startswith('/opt/sil/python/')
    assert installation['source_tree'] is False
    assert installation['exporter'] is False
    assert installation['independent_importer'] is False
    assert installation['header'] and installation['schema_generator']
    assert installation['runner'] == 0
    assert installation['generated'] is True
    assert installation['compilers'] == []
    proc, report, evidence = consumer(image, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert report['passed'] is True
    assert {n: c['status'] for n, c in report['matrices']['nominal']['cases'].items()} == {
        'replay-native': 'pass', 'replay-fmu': 'pass',
        'closed-native': 'pass', 'closed-fmu': 'pass'}
    assert report['matrices']['controls']['exit_code'] == 1
    assert not list(evidence.rglob('core'))
    assert not list(evidence.rglob('*.core'))
    for matrix in ('nominal', 'controls'):
        assert (evidence / matrix / 'junit.xml').is_file()
        assert all(c['passed'] for c in report['matrices'][matrix]['cases'].values())
    for name in ('replay-native', 'replay-fmu', 'closed-native', 'closed-fmu'):
        root = evidence / 'nominal/cases' / name
        assert list(root.rglob('run-1.provenance.json'))
        assert list(root.rglob('run-2.mcap'))
        assert list(root.rglob('independent.comparison.json'))


def test_ignored_failure_exit_codes_cannot_make_acceptance_green(image, tmp_path):
    proc, report, _ = consumer(image, tmp_path, optional_controls=True)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert report['passed'] is False
    assert report['matrices']['nominal']['passed'] is True
    controls = report['matrices']['controls']
    assert controls['exit_code'] == 0
    assert controls['passed'] is False
    # The faults were still detected; ignoring their matrix exit is what fails.
    assert all(c['passed'] for c in controls['cases'].values())


def test_stale_run_owned_files_cannot_satisfy_a_later_control(image):
    proc = subprocess.run([
        'docker', 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
        '--entrypoint', 'python', image, '-c',
        "import json,pathlib,subprocess; "
        "d=json.loads(pathlib.Path('/bundles/control-hang/bundle.json').read_text()); "
        "p=pathlib.Path(d['runtime']['environment']['TMPDIR']); "
        "p.mkdir(parents=True); (p/'native-callback-entered').write_text('native fault callback entered\\n'); "
        "r=subprocess.run(['python','/opt/adas/run.py'],capture_output=True,text=True); "
        "print(json.dumps({'exit':r.returncode,'diagnostic':r.stderr,"
        "'matrix_started':pathlib.Path('/work/evidence/nominal').exists()}))"
    ], check=True, capture_output=True, text=True)
    report = json.loads(proc.stdout)
    assert report['exit'] == 1
    assert 'leftover files from an earlier execution' in report['diagnostic']
    assert report['matrix_started'] is False
