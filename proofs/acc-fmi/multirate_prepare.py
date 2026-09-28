"""Pin ACC FMUs, authored schedules, independent runs and envelope before Runs."""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from xml.etree import ElementTree as ET
from pathlib import Path

from multirate_contract import (FAULT_ROW, FAULTS, FMU_DIR, MODELS, ROWS,
                                declaration, coupling, pinned_schedule)
from multirate_independent import run as independent_run
from proof_support import file_sha256, write_json


def pin_archive(model: str, out: Path) -> dict:
    source = FMU_DIR / f'{model}.fmu'
    target = out / 'fmus' / source.name
    with zipfile.ZipFile(source) as archive:
        description = ET.fromstring(archive.read('modelDescription.xml'))
    co_simulation = description.find('CoSimulation')
    variables = description.find('ModelVariables')
    if (co_simulation is None or variables is None or
            co_simulation.get('canHandleVariableCommunicationStepSize') != 'false' or
            any(variable.tag == 'Clock' for variable in variables)):
        raise RuntimeError(f'{model}: not the pinned fixed-Period scalar profile')
    # Each Run uses one constant Period for each FMU. These source models
    # impose no separate fixed Period in their descriptions; FMPy executes
    # every tested 10/20 ms combination before the bundle is pinned.
    shutil.copyfile(source, target)
    metadata = json.loads((FMU_DIR / f'{model}.identity.json').read_text())
    return {"sha256": file_sha256(target), "exporter_identity": metadata}


def prepare(out: Path):
    (out / 'fmus').mkdir(parents=True, exist_ok=True)
    (out / 'independent').mkdir(exist_ok=True)
    identity = {model: pin_archive(model, out) for model in MODELS.values()}
    write_json(out / 'contract.json', declaration())
    for row in ROWS:
        schedule = pinned_schedule(row)
        independent = independent_run(row)
        # Two derivations of one delivery rule: the authored formula and the
        # Messages FMPy actually consumed. json round-trips the integer keys.
        if json.loads(json.dumps(independent['schedule'])) != schedule:
            raise RuntimeError(f'{row.name}: FMPy delivery differs from the authored schedule')
        write_json(out / f'{row.name}.coupling.json', coupling(row))
        write_json(out / 'independent' / f'{row.name}.json', independent)
        write_json(out / 'independent' / f'{row.name}.schedule.json', schedule)
    for fault in FAULTS:
        write_json(out / 'independent' / f'fault-{fault}.json',
                   independent_run(FAULT_ROW, fault))
    index = {'format': 2, 'models': identity,
             'files': {str(p.relative_to(out)): file_sha256(p)
                       for p in sorted(out.rglob('*')) if p.is_file()}}
    write_json(out / 'bundle.json', index)
    return index


if __name__ == '__main__':
    prepare(Path(sys.argv[1]).resolve())
