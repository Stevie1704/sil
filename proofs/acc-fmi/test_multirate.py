"""The independently stated delivery and observation contract (#197)."""
from dataclasses import replace

import pytest

from multirate_compare import Difference, compare_reference, first_difference, sensitivity
from multirate_contract import (DURATION_NS, GRID_NS, ROWS, CONTROLS,
                                delivery_schedule, observation_times)


def ms(value):
    return value * 1_000_000


def test_different_periods_hold_until_delivery_and_keep_last_endpoint():
    slow_plant = ROWS[1]
    assert {t // ms(1): None if p is None else p // ms(1)
            for t, p in list(delivery_schedule(slow_plant, 'sensing').items())[:6]} == {
                0: None, 10: 0, 20: 0, 30: 20, 40: 20, 50: 40}
    slow_controller = ROWS[2]
    assert {t // ms(1): None if p is None else p // ms(1)
            for t, p in list(delivery_schedule(slow_controller, 'command').items())[:6]} == {
                0: None, 10: 0, 20: 0, 30: 20, 40: 20, 50: 40}
    for row in ROWS:
        for channel in ('sensing', 'command', 'state'):
            assert observation_times(row, channel)[-1] == DURATION_NS
            assert GRID_NS in observation_times(row, channel)


def test_zero_latency_shared_slot_and_wrong_order():
    zero = ROWS[3]
    sensing = delivery_schedule(zero, 'sensing')
    assert [sensing[ms(t)] for t in (0, 10, 20, 30, 40)] == [
        ms(0), ms(0), ms(20), ms(20), ms(40)]
    assert delivery_schedule(ROWS[1], 'sensing')[ms(0)] is None
    assert delivery_schedule(zero, 'command')[ms(0)] is None


def test_first_difference_names_signal_and_endpoint():
    row = replace(ROWS[3], name='short')
    values = {channel: {t: [1.0] * width for t in range(0, DURATION_NS,
              row.periods()['controller' if channel == 'command' else 'plant'])}
              for channel, width in (('sensing', 3), ('state', 3), ('command', 1))}
    altered = {channel: {t: v.copy() for t, v in slots.items()}
               for channel, slots in values.items()}
    altered['sensing'][0][0] = 2.0
    assert first_difference(altered, values, row)['signal'] == 'sensing.gap_m'
    assert first_difference(altered, values, row)['observation_ns'] == ms(20)
    altered['command'][0][0] = 3.0
    assert first_difference(altered, values, row)['signal'] == 'command.accel_mps2'
    assert first_difference(altered, values, row)['observation_ns'] == ms(10)
    altered['command'][0][0] = 1.0
    with pytest.raises(Difference, match='sensing.gap_m.*20000000'):
        compare_reference(altered, {'outputs': values}, row)
    assert sensitivity(values, row, values, row)['inside_envelope']
    outside = {channel: {t: v.copy() for t, v in slots.items()}
               for channel, slots in values.items()}
    for payload in outside['sensing'].values():
        payload[0] += 10.0
    judged = sensitivity(outside, row, values, row)
    assert not judged['inside_envelope']
    assert judged['maxima']['gap_m']['absolute_difference'] == 10.0
