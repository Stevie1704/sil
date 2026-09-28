"""The independently stated delivery and observation contract (#197)."""
from dataclasses import replace

import pytest

from multirate_acceptance import check_plan_schedule
from multirate_compare import (Difference, compare_independent, first_difference,
                               sensitivity)
from multirate_contract import (DURATION_NS, FIELDS, GRID_NS, MS, ROW, ROWS,
                                delivery_schedule, sample_times, varied)


def in_ms(schedule, count):
    return {t // MS: None if p is None else p // MS
            for t, p in list(schedule.items())[:count]}


def test_different_periods_hold_until_delivery_and_keep_last_sample_time():
    assert in_ms(delivery_schedule(ROW['plant-20'], 'sensing'), 6) == {
        0: None, 10: 0, 20: 0, 30: 20, 40: 20, 50: 40}
    assert in_ms(delivery_schedule(ROW['controller-20'], 'command'), 6) == {
        0: None, 10: 0, 20: 0, 30: 20, 40: 20, 50: 40}
    for row in ROWS:
        for channel in FIELDS:
            assert sample_times(row, channel)[-1] == DURATION_NS
            assert GRID_NS in sample_times(row, channel)


def test_zero_latency_shared_slot_follows_slot_order():
    zero = ROW['zero-sensing']
    assert in_ms(delivery_schedule(zero, 'sensing'), 5) == {
        0: 0, 10: 0, 20: 20, 30: 20, 40: 40}
    assert delivery_schedule(ROW['plant-20'], 'sensing')[0] is None
    # A zero-Latency command would run after the plant in the shared Slot.
    zero_command = replace(zero, sensing_ms=10, command_ms=0)
    assert in_ms(delivery_schedule(zero_command, 'command'), 3) == {
        0: None, 20: 10, 40: 30}


def test_zero_sensing_change_reaches_the_plant():
    """The plant takes even-Slot commands, which depend on the sensing Latency."""
    zero, delayed = ROW['zero-sensing'], ROW['command-20']
    assert delivery_schedule(zero, 'command')[40 * MS] == 20 * MS
    assert delivery_schedule(zero, 'sensing')[20 * MS] == 20 * MS
    assert delivery_schedule(delayed, 'sensing')[20 * MS] == 0


def test_every_row_varies_one_period_or_latency_from_its_baseline():
    assert {row.name: varied(row) for row in ROWS if row.baseline} == {
        'plant-20': 'plant_ms', 'controller-20': 'controller_ms',
        'sensing-20': 'sensing_ms', 'command-20': 'command_ms',
        'command-30': 'command_ms', 'zero-sensing': 'sensing_ms'}
    with pytest.raises(ValueError, match='differs from equal-10'):
        varied(replace(ROW['plant-20'], sensing_ms=20))


def test_first_difference_names_signal_and_sample_time():
    row = replace(ROW['zero-sensing'], name='short')
    values = {channel: {t: [1.0] * len(names)
                        for t in range(0, DURATION_NS, row.publication_period(channel))}
              for channel, names in FIELDS.items()}
    altered = {channel: {t: v.copy() for t, v in slots.items()}
               for channel, slots in values.items()}
    altered['sensing'][0][0] = 2.0
    assert first_difference(altered, values, row)['signal'] == 'sensing.gap_m'
    assert first_difference(altered, values, row)['sample_time_ns'] == 20 * MS
    altered['command'][0][0] = 3.0
    assert first_difference(altered, values, row)['signal'] == 'command.accel_mps2'
    assert first_difference(altered, values, row)['sample_time_ns'] == 10 * MS
    altered['command'][0][0] = 1.0
    with pytest.raises(Difference, match='sensing.gap_m.*20000000'):
        compare_independent(altered, {'outputs': values}, row)
    assert sensitivity(values, row, values, row)['inside_envelope']


@pytest.mark.parametrize('channel, field', [
    ('sensing', 'relative_speed_mps'), ('state', 'lead_position_m')])
def test_sensitivity_judges_every_field(channel, field):
    row = ROW['zero-sensing']
    values = {name: {t: [1.0] * len(names)
                     for t in range(0, DURATION_NS, row.publication_period(name))}
              for name, names in FIELDS.items()}
    outside = {name: {t: v.copy() for t, v in slots.items()}
               for name, slots in values.items()}
    for payload in outside[channel].values():
        payload[FIELDS[channel].index(field)] += 1.0
    judged = sensitivity(outside, row, values, row)
    assert not judged['inside_envelope']
    assert judged['maxima'][field]['absolute_difference'] == 1.0


def test_independent_schedule_rejects_wrong_same_slot_plan():
    zero = ROW['zero-sensing']
    receipt = {'plan': {'routes': [
        {'channel': 'sensing', 'activations': [
            {'at_ns': 0, 'input': {'published_ns': 0}}]},
        {'channel': 'command', 'activations': [
            {'at_ns': 0, 'input': 'start'}]},
    ]}}
    assert check_plan_schedule(receipt, zero) == {'sensing': 1, 'command': 1}
    receipt['plan']['routes'][0]['activations'][0]['input'] = 'start'
    with pytest.raises(RuntimeError, match='sensing at 0'):
        check_plan_schedule(receipt, zero)
