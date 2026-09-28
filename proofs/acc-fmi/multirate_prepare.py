"""Pin ACC FMUs, authored schedules, references and envelope before Runs."""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from xml.etree import ElementTree as ET
from pathlib import Path

from multirate_contract import (ATOL, CONTROLS, DURATION_NS, ENVELOPE, FIELDS,
                                GRID_NS, ROWS, RTOL, coupling, delivery_schedule)
from multirate_reference import run as independent_run
from proof_support import file_sha256, write_json


def prepare(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    (out / 'fmus').mkdir(exist_ok=True)
    (out / 'references').mkdir(exist_ok=True)
    identity = {}
    for model in ('AccController', 'AccPlant'):
        source = Path('/fmus') / f'{model}.fmu'
        target = out / 'fmus' / source.name
        with zipfile.ZipFile(source) as archive:
            description = ET.fromstring(archive.read('modelDescription.xml'))
        co_simulation = description.find('CoSimulation')
        variables = description.find('ModelVariables')
        if (co_simulation is None or variables is None or
                co_simulation.get('canHandleVariableCommunicationStepSize') != 'false' or
                any(variable.tag == 'Clock' for variable in variables)):
            raise RuntimeError(f'{model}: not the pinned fixed-interval scalar profile')
        # Each Run uses one constant interval for each FMU. These source models
        # impose no separate fixed interval in their descriptions; FMPy executes
        # every tested 10/20 ms combination before the bundle is pinned.
        shutil.copyfile(source, target)
        metadata = json.loads((Path('/fmus') / f'{model}.identity.json').read_text())
        identity[model] = {"sha256": file_sha256(target), "exporter_identity": metadata}
    write_json(out / 'contract.json', {
        'rows': [row.__dict__ for row in ROWS],
        'controls': [row.__dict__ for row in CONTROLS],
        'duration_ns': DURATION_NS, 'common_grid_ns': GRID_NS,
        'fields': FIELDS, 'atol': ATOL, 'rtol': RTOL,
        'sensitivity_envelope': ENVELOPE,
        'initialization': {"plant": {"accel_mps2": 0.0, "lead_accel_mps2": 0.0,
                                     "initial_lead_position_m": 60.0},
                           "controller": {"gap_m": 60.0, "relative_speed_mps": 0.0,
                                          "ego_speed_mps": 25.0}},
        'hold_policy': 'latest delivered Message; input start until first delivery',
        'endpoint_mapping': 'FMU output observation = publication Slot + publisher Period',
        'duration_policy': 'half-open publication Slots; last endpoint equals Duration',
    })
    for row in (*ROWS, *CONTROLS):
        write_json(out / f'{row.name}.coupling.json', coupling(row))
        write_json(out / 'references' / f'{row.name}.json', independent_run(row))
        write_json(out / 'references' / f'{row.name}.schedule.json', {
            channel: {str(t): publication for t, publication in delivery_schedule(row, channel).items()}
            for channel in ('sensing', 'command')})
    zero = next(row for row in ROWS if row.name == 'zero-sensing')
    write_json(out / 'references' / 'wrong-zero-order.json',
               independent_run(zero, wrong_zero_order=True))
    index = {'format': 1, 'models': identity,
             'files': {str(p.relative_to(out)): file_sha256(p)
                       for p in sorted(out.rglob('*')) if p.is_file()}}
    write_json(out / 'bundle.json', index)
    return index


if __name__ == '__main__':
    prepare(Path(sys.argv[1]).resolve())
