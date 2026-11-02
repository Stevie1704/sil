"""Offline Activation selection and an evidence-only Recording handoff."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from sil.diagnostics_html_markup import Html, code, join, section, table, tag, void
from sil.diagnostics_report import ReportError, parse_report
from sil.profile_set import ProfileSet, ProfileSetError, _read_bounded

NAVIGATION_LIMIT = 200_000
PAGE_SIZE = 100


def select_activations(profile: ProfileSet, *, participant=None, task=None,
                       index=None, start=None, end=None) -> ProfileSet:
    if task is not None and participant is None:
        raise ProfileSetError('--task requires --participant')
    if index is not None and (participant is None or task is None or index < 0):
        raise ProfileSetError('--activation-index requires Participant and Task and must be nonnegative')
    if index is not None and (start is not None or end is not None):
        raise ProfileSetError('--activation-index cannot be combined with a time range')
    if (start is not None and start < 0) or (end is not None and end < 0):
        raise ProfileSetError('Virtual time bounds must be nonnegative integer nanoseconds')
    if start is not None and end is not None and start >= end:
        raise ProfileSetError('Virtual time range must have --from less than --to')
    if all(v is None for v in (participant, task, index, start, end)):
        return profile
    if profile.correlation is None:
        raise ProfileSetError('offline Activation selection requires validated CPU correlation')
    activations = profile.correlation['activations']
    candidates = [(i, a) for i, a in enumerate(activations)
                  if (participant is None or a['participant'] == participant)
                  and (task is None or a['task'] == task)]
    known = {(t['participant'], t['task']) for t in
             (profile.diagnostics or {}).get('observational', {}).get('tasks', [])}
    if not candidates and not any(p == participant and (task is None or t == task) for p, t in known):
        raise ProfileSetError('Participant / Task is not present in the retained evidence')
    refs = tuple(i for i, a in candidates
                 if (index is None or a['index'] == index)
                 and (start is None or a['virtual_time_ns'] >= start)
                 and (end is None or a['virtual_time_ns'] < end))
    label = f"Participant {participant or 'all'}, Task {task or 'all'}"
    if index is not None:
        label += f', Activation index {index}'
    else:
        label += f", Virtual time [{start if start is not None else 'start'}, {end if end is not None else 'end'}) ns"
    return replace(profile, selection=refs, selection_label=label)


def with_diagnostics(profile: ProfileSet, path: Path | None) -> ProfileSet:
    if path is None:
        return profile
    expected = profile.report['run'].get('diagnostics_report')
    artifact = next((a for a in profile.report['artifacts']
                     if a['path'] == expected and a['role'] == 'diagnostics_report'), None)
    if artifact is None:
        raise ProfileSetError('profile has no diagnostics artifact identity to verify')
    try:
        data = _read_bounded(path, str(path))
        if artifact['bytes'] != len(data) or artifact['sha256'] != hashlib.sha256(data).hexdigest():
            raise ProfileSetError('diagnostics size/SHA-256 does not match this profile Run')
        diagnostics = parse_report(data.decode('utf-8'), source=str(path))
    except (OSError, UnicodeDecodeError, ReportError) as error:
        raise ProfileSetError(f'invalid diagnostics: {error}') from error
    if 'sil_run_diagnostics' not in diagnostics:
        raise ProfileSetError('diagnostics input must be one Run report')
    report = profile.report
    for key in ('manifest_hash', 'sil_version', 'source_repository', 'source_revision'):
        a, b = report['identity'].get(key), diagnostics['identity'].get(key)
        if key == 'manifest_hash':
            a, b = (a or '').removeprefix('sha256:'), (b or '').removeprefix('sha256:')
        if a != b:
            raise ProfileSetError(f'diagnostics identity.{key} differs from the profile')
    for key in ('participant_timeout_ms', 'max_protocol_line_bytes',
                'max_step_inline_payload_bytes', 'max_step_output_messages', 'provenance_mode'):
        # A null guard in a profile means the runner default was requested.
        # Diagnostics records the effective defaults; only explicit overrides compare.
        if report['configuration'].get(key) is not None and report['configuration'][key] != diagnostics['configuration'].get(key):
            raise ProfileSetError(f'diagnostics configuration.{key} differs from the profile')
    for key in ('os', 'architecture'):
        if report['host'].get(key) != diagnostics['host'].get(key):
            raise ProfileSetError(f'diagnostics host.{key} differs from the profile')
    if (report['configuration']['recording'] is not None) != diagnostics['configuration']['recording']:
        raise ProfileSetError('diagnostics Recording configuration differs from the profile')
    if report['run']['exit_code'] != diagnostics['run']['exit_code']:
        raise ProfileSetError('diagnostics Run outcome differs from the profile')
    _check_diagnostics_intervals(profile, diagnostics)
    return replace(profile, diagnostics=diagnostics)


def _check_diagnostics_intervals(profile: ProfileSet, diagnostics: dict) -> None:
    if profile.correlation is None:
        return
    tasks = {(t['participant'], t['task']): t
             for t in diagnostics['observational']['tasks']}
    retained = {}
    completed = {}
    for a in profile.correlation['activations']:
        key = (a['participant'], a['task'])
        task = tasks.get(key)
        if task is None or task['kind'] != a['kind']:
            raise ProfileSetError('retained Activation has no matching diagnostics Task')
        identity = (*key, a['index'])
        retained[identity] = a
        if a['outcome'] == 'completed':
            completed[key] = completed.get(key, 0) + 1
    for key, task in tasks.items():
        timing = task['timing']
        if completed.get(key, 0) > timing['completed']:
            raise ProfileSetError('retained completed intervals exceed diagnostics measurement count')
        for outlier in timing['slowest']:
            a = retained.get((*key, outlier['activation_index']))
            if a is not None and (a['outcome'] != 'completed' or
                    a['virtual_time_ns'] != outlier['virtual_time_ns'] or
                    int(a['end_ns']) - int(a['begin_ns']) != outlier['wall_ns']):
                raise ProfileSetError('retained outlier interval differs from matched diagnostics')


def activation_rows(profile: ProfileSet) -> list[tuple[int, dict]]:
    if profile.correlation is None:
        return []
    refs = profile.selection
    activations = profile.correlation['activations']
    return (list(enumerate(activations)) if refs is None
            else [(i, activations[i]) for i in refs])


def query_recipe(profile: ProfileSet, virtual: int | None = None) -> str:
    artifact = next((a for a in profile.report['artifacts'] if a['role'] == 'recording'), None)
    digest = ('Expected Recording SHA-256: ' + artifact['sha256'] if artifact
              else 'Recording link unavailable/unverified: no retained digest.')
    time = (f'Activation Virtual time: {virtual} ns.' if virtual is not None
            else 'Activation Virtual time: unavailable (no interval selected).')
    return (f'{time} {digest}\n'
            'Choose the Recording path, Channel, fields and publication-time interval explicitly.\n'
            'sil recording query RECORDING.mcap --channel CHANNEL --field FIELD '
            '--from FROM_NS --to TO_NS --max-results 100 --max-bytes 65536 --max-messages 10000\n'
            'Replace FROM_NS/TO_NS with exact integer nanoseconds for [from,to). '
            'Compare the query report source.sha256 against the expected digest. '
            'A missing/moved Recording must be located and verified first. '
            'Publication time and payload Sample time differ; equal Virtual time does not identify a Message or cause.')


def navigation_data(profile: ProfileSet) -> list | None:
    rows = activation_rows(profile)
    if len(rows) > NAVIGATION_LIMIT:
        return None
    return [[i, a['participant'], a['task'], str(a['index']), str(a['virtual_time_ns']),
             str(int(a['end_ns']) - int(a['begin_ns'])), a['outcome'], a['samples'],
             a['weight_ns']]
            for i, a in rows]


def navigation_section(profile: ProfileSet) -> Html:
    if profile.correlation is None and profile.diagnostics is None:
        return Html('')
    rows = activation_rows(profile)
    completed = [(i, a) for i, a in rows if a['outcome'] == 'completed']
    slowest = sorted(completed, key=lambda r: -(int(r[1]['end_ns']) - int(r[1]['begin_ns'])))[:20]
    headers = [('Participant / Task', False), ('Activation index', True),
               ('Virtual time (ns)', True), ('Elapsed wall time (ns)', True),
               ('Samples', True), ('Sampled weight (ns)', True)]
    def cells(i, a):
        return [tag('button', f"{a['participant']} / {a['task']}", type='button',
                    data_interval_kind='activation', data_interval_ref=str(i)),
                str(a['index']), code(str(a['virtual_time_ns'])),
                code(str(int(a['end_ns']) - int(a['begin_ns']))),
                str(a['samples']), str(a['weight_ns'])]
    body = [tag('p', 'Completed retained intervals only; inclusive host elapsed time is distinct from sampled weight. '
                    'Failed/unclosed work is excluded from duration ranking. No full-Run percentiles are inferred. '
                    'Unknown observation gaps and zero samples remain unknown.'),
            table('slowest-retained', 'Slowest retained completed Activations (up to 20)', headers,
                  [cells(i, a) for i, a in slowest])]
    if profile.selection is not None:
        selected = rows[:500]
        body += [section('activation-selection', 'Offline Activation selection',
                  tag('p', profile.selection_label),
                  tag('p', 'Selected interval missing from retained correlation; no nearby interval substituted.')
                  if not rows else None,
                  table('selected-activations', 'Selected Activations', headers, [cells(i, a) for i, a in selected]),
                  *(tag('details', tag('summary', f"{a['participant']} / {a['task']} index {a['index']}: "
                        f"{a['samples']} samples / {a['weight_ns']} ns sampled weight"),
                        tag('textarea', query_recipe(profile, a['virtual_time_ns']), rows='7',
                            aria_label='Editable bounded Recording query recipe')) for _, a in selected[:20]),
                  tag('p', f'{max(0, len(rows)-500)} selected intervals omitted from the static table; narrow offline selection for more.'))]
    body += [tag('div', tag('label', 'Find Participant / Task ', void('input', id='nav-search', type='search')),
                 tag('label', 'Activation index ', void('input', id='nav-index', type='text', inputmode='numeric')),
                 tag('label', 'Virtual time from (ns) ', void('input', id='nav-from', type='text', inputmode='numeric')),
                 tag('label', 'Virtual time to (ns) ', void('input', id='nav-to', type='text', inputmode='numeric')),
                 tag('label', 'Order ', tag('select', tag('option', 'Stream order', value='stream'),
                         tag('option', 'Slowest completed first', value='duration'), id='nav-order')),
                 tag('button', 'Previous page', id='nav-prev', type='button'),
                 tag('button', 'Next page', id='nav-next', type='button'),
                 tag('p', id='nav-status', aria_live='polite'), tag('div', id='nav-page'),
                 tag('p', id='nav-detail', aria_live='polite'),
                 tag('textarea', id='nav-recipe', rows='7', aria_label='Editable bounded Recording query recipe'),
                 id='activation-navigation', hidden=True),
             tag('p', f'Navigation shows at most {PAGE_SIZE} rows per page; interactive metadata is capped at '
                      f'{NAVIGATION_LIMIT} Activations. Beyond it, use offline --participant/--task/'
                      '--activation-index or --from/--to. Static lists and timeline remain bounded to 500 intervals. '
                      'For any Activation, select Channel/fields and a [from,to) interval in the editable Recording query recipe; '
                      'the query is never executed by this report.')]
    if profile.diagnostics is not None:
        tasks = profile.diagnostics['observational']['tasks']
        selected_tasks = {(a['participant'], a['task']) for _, a in rows}
        retained = {(a['participant'], a['task'], a['index'], a['virtual_time_ns'])
                    for a in (profile.correlation or {}).get('activations', [])}
        diagnostics_rows = []
        for task in tasks:
            if profile.selection is not None and selected_tasks and (task['participant'], task['task']) not in selected_tasks:
                continue
            timing = task['timing']
            diagnostics_rows.append([task['participant'] + ' / ' + task['task'], str(timing['completed']),
                str(timing['p50_ns']), str(timing['p95_ns']), str(timing['p99_ns']),
                str(timing['quantiles']['approximate']),
                tag('details', tag('summary', 'Slowest 20 and reservoir qualification'),
                    code(str(timing['quantiles'])),
                    tag('p', 'Measurement scope: ', timing['scope'], '; unavailable: ', code(str(timing['unavailable']))),
                    *[tag('p', str(s), '; retained interval ',
                          'present' if (task['participant'], task['task'], s['activation_index'], s['virtual_time_ns'])
                              in retained else 'missing (no nearby interval substituted)')
                      for s in timing['slowest'][:20]])])
        body += [table('diagnostics-timings', 'Explicit matched diagnostics: full measurement scope, reservoir-qualified quantiles',
                       [('Task', False), ('Completed', True), ('p50 ns', True), ('p95 ns', True), ('p99 ns', True),
                        ('Approximate', False), ('Scope and outliers', False)], diagnostics_rows[:500]),
                 tag('p', f'{max(0,len(diagnostics_rows)-500)} diagnostics Tasks omitted; null means unavailable, not zero.')]
    return section('activation-outliers', 'Activation outliers and navigation', *body)
