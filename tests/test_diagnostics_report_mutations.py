"""The validator, not the renderer, is the contract of a saved report (#304).

Every frozen example is mutated one member at a time: the member is deleted
or given a value of another JSON type. Each variant must either be rejected
with a ReportError, so `sil diagnostics render` exits 2, or be valid, and
then the HTML page and the terminal summary must come out of it. Any other
exception on a validated report is a renderer bug.
"""

import copy
import json

import pytest

from conftest import ROOT
from sil.benchmark import format_summary as benchmark_summary
from sil.diagnostics_html import render
from sil.diagnostics_report import (
    BENCHMARK_REPORT_KEY, ReportError, parse_report,
)
from sil.diagnostics_run import format_summary as run_summary
from pathlib import Path

FIXTURES = ROOT / "tests" / "fixtures" / "diagnostics"
EXAMPLES = sorted(path.name for path in FIXTURES.glob("*.json"))
# Values of every JSON type, so each member also gets the types it lacks.
REPLACEMENTS = (None, "x", 0, -1, 1.5, True, [], {})
# Members of a long list are alike, so only the first ones are mutated.
LIST_MEMBERS_MUTATED = 2


def leaves(value, path=()):
    """Every member path of `value`, containers included."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield path + (key,)
            yield from leaves(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value[:LIST_MEMBERS_MUTATED]):
            yield path + (index,)
            yield from leaves(item, path + (index,))


def variants(document):
    """Every single-member deletion and retyping of `document`."""
    for path in list(leaves(document)):
        mutated = copy.deepcopy(document)
        parent = mutated
        for key in path[:-1]:
            parent = parent[key]
        last = path[-1]
        original = parent[last]
        for replacement in REPLACEMENTS:
            if type(replacement) is type(original) and replacement == original:
                continue
            parent[last] = replacement
            yield path, repr(replacement), json.dumps(mutated)
        if isinstance(parent, dict):
            del parent[last]
            yield path, "deleted", json.dumps(mutated)


def present(text: str) -> None:
    """What `sil diagnostics render` and the summaries do with a report."""
    try:
        document = parse_report(text)
    except ReportError:
        return
    render(text)
    if BENCHMARK_REPORT_KEY in document:
        benchmark_summary(document, Path("bench"))
    else:
        run_summary(document, Path("run.json"))


@pytest.mark.parametrize("name", EXAMPLES)
def test_a_mutated_report_is_rejected_or_presented(name):
    document = json.loads((FIXTURES / name).read_text())
    crashes = []
    for path, change, text in variants(document):
        try:
            present(text)
        except Exception as error:  # noqa: BLE001
            crashes.append(f"{'.'.join(map(str, path))} {change}: "
                           f"{type(error).__name__}: {error}")
    assert not crashes, f"{len(crashes)} crashes:\n" + "\n".join(crashes[:40])
