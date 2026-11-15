# Choose an integration

[Documentation index](README.md) · [Repository overview](../README.md)

Select the guide from what you have. Each guide has a worked example that
`sil examples --dir examples` exports from an installed SiL. To see how the
kinds of Participant connect to the kernel, read
[How SiL works](how-sil-works.md#runtime-structure).

| You have | It joins a Run as | Read |
| --- | --- | --- |
| An FMU (FMI 3.0, or FMI 2.0 co-simulation) | A Process participant that the Importer drives | [FMI importer](fmi.md) |
| Several FMUs that exchange values | One Process participant per FMU, coupled through Channels | [Coupling and substituting FMUs](fmu-coupling.md) |
| A C library with its own init and step API | A Process participant with a generated adapter | [Shared-library participants](library.md) |
| A C application that you build against SiL | A Native participant through the C ABI | [Native ADAS reference application](adas-reference.md), [Native participant alternative](library.md#native-participant-alternative) |
| An executable in any language | A Process participant that speaks the Step protocol | [Step protocol](step-protocol.md), [Start a Participant](running.md#start-a-participant) |
| Recorded data as timestamped CSV | A Replay participant, after `sil recording csv` | [Recorded input and FMU replay](recorded-input.md) |
| A Recording of an earlier Run | A Replay participant, optionally for a window after a warm-up | [Replay](replay.md) |

## Select Native or Process

A Native participant runs in the kernel's process: it has no protocol cost,
but a crash or a hang ends the Run without cleanup, and it has no Clock
shim. A Process participant runs in its own process, under a response
deadline, and with `shim=True` the Clock shim answers its clock reads from
Virtual time. The
[comparison table](library.md#native-participant-alternative) gives the
details. For an existing library or application that you cannot change,
start with a Process participant.

## With a coding agent

The [agent guide](agent-guide.md#onboard-a-sut) gives the same choice as
steps for a coding agent. Run `sil skill install` in your project so the
agent finds the guide of your installed version.
