"""Importer for FMI 3.0 co-simulation FMUs, run as a process participant.

The kernel learns nothing about FMI. A Manifest declares this module as a
process participant's command with an FMU path, and the step protocol drives
the FMU's co-simulation interface: its input variables are written from the
subscribed Channels, `fmi3DoStep` advances it, and its output variables are
published on the Channels it publishes.

A Float64 mapping needs no configuration beyond the Manifest. The
initialization line carries each Channel's schema and its direction, so a
Channel's schema field names are the FMU variable names and the direction
decides which side of the step the variable is touched on.

Every other variable is *declared* rather than derived, because the FMU's
vocabulary and the Channel's are not the same one and a schema is typically
carried by more than one Channel:

    python -m sil.fmi <model.fmu> --bind <channel>:<field>=<variable> ...
                                  --start <variable>=<value> ...

Declaring one binding declares them all: the bindings are then the whole
mapping, and a Channel field that none of them names is a mapping mistake
rather than a variable left at zero.

A Binary variable is variable-length and a Message is not, so a Channel
carries a Binary value in an explicit bounded representation: a `u8` array
field holding the payload and the `<field>_length` field beside it holding how
much of it is the payload. On every Message this importer publishes, the bytes
above that length are zero, so one Manifest records the same bytes twice; on
an incoming Message they are ignored, because the length is what says where
the payload ends. Nothing is ever truncated to fit — a payload above the
bound, in either direction, aborts the Run.

A Binary variable that declares a Clock is not a value the Step reads: it is
defined only while its Clock is active, which happens in Event Mode. Such a
Channel carries one Message per Clock activation and a third field,
`<field>_event_time_ns`, holding the FMI event time that activation belongs
to. That time is not the Channel's: a Message is published in the Slot the
importer's activation runs in, becomes visible to a subscriber one Latency
later, and states the FMI event time it carries rather than being timestamped
with it.

Both the bindings and the start values travel as command arguments, which the
Manifest already hashes, so nothing that affects the Run lives outside the
hashed Manifest.

The importer is a package of small modules, and the direction between them is
one way. `description` reads the archive; `runtime` drives the native library
and owns every buffer and pointer; `binding` moves a Channel's fields across
that seam and `mapping` resolves which fields those are; `stepping` states what
both coordinators obey around an event; `single` and `group` are the two
scheduling policies, `terminals` and `composition` the group's own members and
how they are declared; `archive` unpacks the FMU and `cli` chooses between the
two participants. What a Manifest names — `python -m sil.fmi` — and what
another module imports — `sil.fmi.FmuParticipant` and the rest below — is the
same surface it has always been.
"""

from sil.fmi.cli import main
from sil.fmi.description import (
    BusProfile,
    ModelDescription,
    Terminal,
    Variable,
    library_suffix,
    platform_directory,
)
from sil.fmi.group import FmuGroupParticipant
from sil.fmi.runtime import NS_PER_S, CoSimulation
from sil.fmi.single import FmuParticipant

__all__ = [
    "NS_PER_S",
    "BusProfile",
    "CoSimulation",
    "FmuGroupParticipant",
    "FmuParticipant",
    "ModelDescription",
    "Terminal",
    "Variable",
    "library_suffix",
    "main",
    "platform_directory",
]
