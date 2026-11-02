"""Activation selection through the offline render command and HTML."""
import json

import pytest

from profile_sets import artifact_set, correlation_document, fixture, samples_document
from test_diagnostics_html import Page
from test_profile_render import render


def late_set(directory):
    report = fixture('activation-complete.json')
    task = report['activation_correlation']['summary']['tasks'][0]
    task['activations'] += 600
    task['completed'] += 600
    task['without_samples'] += 600
    report['activation_correlation']['summary']['intervals']['activations'] += 600
    samples = samples_document(report)
    correlation = correlation_document(report, samples)
    late = correlation['activations'][600]
    late['virtual_time_ns'] = 9007199254740993
    late['end_ns'] = str(int(late['begin_ns']) + 987654321)
    return artifact_set(directory, 'activation-complete.json', report=report,
                        samples=samples, correlation=correlation)


def test_offline_selection_reaches_late_zero_sample_outlier(tmp_path):
    profile = late_set(tmp_path / 'set')
    result = render([str(profile), '--participant', 'controller', '--task', 'tick',
                     '--activation-index', '600', '--html', 'selected.html'], tmp_path)
    assert result.returncode == 0, result.stderr
    html = (tmp_path / 'selected.html').read_text()
    selected = html.split('id="activation-selection"', 1)[1].split('</section>', 1)[0]
    text = Page(selected).text
    assert '9007199254740993' in text
    assert '987654321' in text
    assert '0 samples / 0 ns sampled weight' in text
    assert 'sil recording query' in text
    assert '--max-messages' in text
    assert 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' in text


@pytest.mark.parametrize('flags', [
    ['--task', 'tick'], ['--activation-index', '1'],
    ['--participant', 'controller', '--task', 'tick', '--activation-index', '-1'],
    ['--participant', 'controller', '--task', 'tick', '--activation-index', '0', '--from', '0'],
    ['--from', '2', '--to', '1'], ['--from', '1.5'],
])
def test_ambiguous_selectors_fail_before_output(tmp_path, flags):
    profile = artifact_set(tmp_path / 'set', 'activation-complete.json')
    result = render([str(profile), *flags, '--html', 'bad.html'], tmp_path)
    assert result.returncode == 2
    assert not (tmp_path / 'bad.html').exists()

def test_exact_half_open_virtual_time_selection_keeps_all_equal_time_tasks(tmp_path):
    profile = artifact_set(tmp_path / 'set', 'activation-complete.json')
    result = render([str(profile), '--from', '10000000', '--to', '20000000',
                     '--html', 'window.html'], tmp_path)
    assert result.returncode == 0, result.stderr
    html = (tmp_path / 'window.html').read_text()
    selected = Page(html.split('id="activation-selection"', 1)[1].split('</section>', 1)[0]).text
    assert 'controller / tick' in selected and 'plant / step' in selected
    assert '10000000' in selected
    assert '30000000' not in selected
    assert 'Selected classes' in Page(html).text


def test_explicit_invalid_diagnostics_is_not_ignored(tmp_path):
    profile = artifact_set(tmp_path / 'set', 'activation-complete.json')
    bad = tmp_path / 'other.json'
    bad.write_text('{}')
    result = render([str(profile), '--diagnostics', str(bad), '--html', 'bad.html'], tmp_path)
    assert result.returncode == 2
    assert 'diagnostics' in result.stderr
    assert not (tmp_path / 'bad.html').exists()

def test_matched_explicit_diagnostics_retains_reservoir_scope_and_absent_intervals(tmp_path):
    from pathlib import Path
    from profile_sets import seal
    report = fixture('cpu-complete.json')
    diagnostics = json.loads((Path(__file__).parent / 'fixtures/diagnostics/run-success.json').read_text())
    diagnostics['identity'].update(report['identity'])
    diagnostics['identity']['manifest_hash'] = report['identity']['manifest_hash'].removeprefix('sha256:')
    report['configuration']['participant_timeout_ms'] = diagnostics['configuration']['participant_timeout_ms']
    profile = artifact_set(tmp_path / 'set', 'cpu-complete.json', report=report)
    path = profile.parent / 'run.diagnostics.json'
    path.write_text(json.dumps(diagnostics))
    seal(profile.parent)
    result = render([str(profile), '--diagnostics', str(path), '--html', 'matched.html'], tmp_path)
    assert result.returncode == 0, result.stderr
    text = Page((tmp_path / 'matched.html').read_text()).text
    assert 'reservoir-qualified quantiles' in text
    assert '8192' in text
    assert 'missing (no nearby interval substituted)' in text
    assert 'insufficient_samples' in text  # null measurements keep their reason


def test_offline_selection_reaches_task_after_first_fifty(tmp_path):
    report = fixture('activation-complete.json')
    summary = report['activation_correlation']['summary']
    for i in range(60):
        summary['tasks'].append({'participant': f'late-{i:02}', 'task': 'work', 'kind': 'native_task',
            'activations': 1, 'completed': 1, 'failed': 0, 'interrupted': 0,
            'with_samples': 0, 'without_samples': 1,
            'execution_context': {'samples': 0, 'weight_ns': 0},
            'window_associated': {'samples': 0, 'weight_ns': 0}})
    summary['intervals']['activations'] += 60
    profile = artifact_set(tmp_path / 'set', 'activation-complete.json', report=report)
    result = render([str(profile), '--participant', 'late-59', '--task', 'work',
                     '--activation-index', '0', '--html', 'task.html'], tmp_path)
    assert result.returncode == 0, result.stderr
    text = Page((tmp_path / 'task.html').read_text()).text
    assert 'Participant late-59, Task work, Activation index 0' in text
    assert '0 samples / 0 ns sampled weight' in text


def test_exact_activation_selection_rejects_duplicate_retained_identities(tmp_path):
    report = fixture('activation-complete.json')
    samples = samples_document(report)
    correlation = correlation_document(report, samples)
    correlation['activations'][1]['index'] = 0
    profile = artifact_set(tmp_path / 'set', 'activation-complete.json', report=report,
                           samples=samples, correlation=correlation)

    result = render([str(profile), '--participant', 'controller', '--task', 'tick',
                     '--activation-index', '0', '--html', 'ambiguous.html'], tmp_path)

    assert result.returncode == 2
    assert 'duplicate Activation identity' in result.stderr
    assert not (tmp_path / 'ambiguous.html').exists()


def selected_text(html):
    return Page(html.split('id="activation-selection"', 1)[1].split('</section>', 1)[0]).text


def test_selected_failed_activation_shows_its_outcome(tmp_path):
    profile = artifact_set(tmp_path / 'set', 'activation-complete.json')
    result = render([str(profile), '--participant', 'plant', '--task', 'step',
                     '--html', 'failed.html'], tmp_path)
    assert result.returncode == 0, result.stderr
    html = (tmp_path / 'failed.html').read_text()
    page = Page(html.split('id="selected-activations"', 1)[1].split('</table>', 1)[0])
    assert 'Outcome' in page.text
    assert 'failed' in page.text and 'completed' in page.text
    # Without the script the interval buttons have no action.
    buttons = [a for t, a in page.elements if t == 'button' and 'data-interval-kind' in a]
    assert buttons and all('disabled' in a for a in buttons)


def test_time_only_selection_without_activations_selects_nothing(tmp_path):
    from dataclasses import replace
    from sil.profile_navigation import select_activations
    from sil.profile_set import load_profile_set
    profile = load_profile_set(artifact_set(tmp_path / 'set', 'activation-complete.json'))
    empty = replace(profile, correlation={**profile.correlation, 'activations': []})
    assert select_activations(empty, start=0).selection == ()


def test_selection_errors_name_the_selection_not_the_profile(tmp_path):
    profile = artifact_set(tmp_path / 'set', 'activation-complete.json')
    result = render([str(profile), '--task', 'tick', '--html', 'bad.html'], tmp_path)
    assert result.returncode == 2
    assert '--task requires --participant' in result.stderr
    assert 'is not a profile' not in result.stderr


@pytest.mark.parametrize('content', [None, '{"other": 1}', '[' * 1_000_000 + ']' * 1_000_000],
                         ids=['missing', 'mismatched', 'nested'])
def test_missing_mismatched_or_nested_diagnostics_fail_before_output(tmp_path, content):
    from profile_sets import seal
    profile = artifact_set(tmp_path / 'set', 'activation-complete.json')
    path = profile.parent / 'run.diagnostics.json'
    if content is not None:
        path.write_text('{}')
        seal(profile.parent)  # the profile binds the '{}' bytes
        if content.startswith('['):
            path.write_text(content)
            seal(profile.parent)
        else:
            path.write_text(content)
    result = render([str(profile), '--diagnostics', str(path), '--html', 'bad.html'], tmp_path)
    assert result.returncode == 2, result.stderr
    assert 'diagnostics' in result.stderr
    assert 'Traceback' not in result.stderr
    assert not (tmp_path / 'bad.html').exists()


def test_recipe_without_a_retained_recording_digest_is_unverified(tmp_path):
    from profile_sets import seal
    profile = artifact_set(tmp_path / 'set', 'activation-complete.json')
    report = json.loads(profile.read_text())
    report['artifacts'] = [a for a in report['artifacts'] if a['role'] != 'recording']
    profile.write_text(json.dumps(report))
    seal(profile.parent)
    result = render([str(profile), '--participant', 'controller', '--task', 'tick',
                     '--activation-index', '0', '--html', 'moved.html'], tmp_path)
    assert result.returncode == 0, result.stderr
    text = selected_text((tmp_path / 'moved.html').read_text())
    assert 'Recording link unavailable/unverified' in text
    assert 'A missing/moved Recording must be located and verified first' in text
