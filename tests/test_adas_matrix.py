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
        "'runner':subprocess.run(['sil-run','--version'],capture_output=True).returncode}))"
    ], check=True, capture_output=True, text=True)
    installation = json.loads(probe.stdout)
    assert installation['uid'] == 10001
    assert installation['sil'].startswith('/opt/sil/python/')
    assert installation['source_tree'] is False
    assert installation['exporter'] is False
    assert installation['independent_importer'] is False
    assert installation['header'] and installation['schema_generator']
    assert installation['runner'] == 0
    proc, report, evidence = consumer(image, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert report['passed'] is True
    assert {n: c['status'] for n, c in report['matrices']['nominal']['cases'].items()} == {
        'replay-native': 'pass', 'replay-fmu': 'pass',
        'closed-native': 'pass', 'closed-fmu': 'pass'}
    assert report['matrices']['controls']['exit_code'] == 1
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
    controls = report['matrices']['controls']
    assert controls['exit_code'] == 0
    assert controls['passed'] is False
    # The faults were still detected; ignoring their matrix exit is what fails.
    assert all(c['passed'] for c in controls['cases'].values())
