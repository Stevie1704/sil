# perf capture-window control: source findings for #361

This is a source-based feasibility input to [#361](https://github.com/Stevie1704/sil/issues/361),
not a Linux qualification result. The inspected implementation is upstream
Linux **v7.0**, matching the upstream release family of the qualification
host's Ubuntu `linux-tools-7.0.0-1017-azure`
`7.0.0-1017.17~24.04.1`. Distribution patches still require runtime checks;
this note does not identify that package with unpatched upstream source.
The repository's CPU collector already uses `cpu-clock:u`, DWARF stacks, `-k monotonic` and a control FIFO; its current
size/reserve path sends a bare `disable` ([implementation](../../python/src/sil/profile_run.py)).

## Start disabled while retaining setup metadata

`--delay=-1` starts sampling events disabled. `--control=fifo:ctl,ack` accepts
enable/disable commands, optionally for one named event, and has a separate
acknowledgment descriptor. Time ranges accepted by `--delay` are host
milliseconds; they do not select SiL Virtual time or an Activation.
[Linux v7.0 perf-record documentation](https://github.com/torvalds/linux/blob/v7.0/tools/perf/Documentation/perf-record.txt)

For an initial delay, `record__config_tracking_events` obtains a dummy
tracking event through `evlist__findnew_tracking_event`. On a launched
workload it sets that event to enable on exec.
This is specifically intended to retain mappings during the sampling delay.
The normal workload launch still executes immediately; `-1` skips the positive
delay's sleep/enable path.
[Linux v7.0 builtin-record.c, `record__config_tracking_events` and `__cmd_record`](https://github.com/torvalds/linux/blob/v7.0/tools/perf/builtin-record.c)

The tracking event requests Task, MMAP/MMAP2, COMM and optional build-ID
records; inheritance and sample-ID metadata are configured separately.
The event's clock ID can be explicitly set. Consequently a proof should
verify setup exec, DSO mappings, process lifetime events and build identities
in the retained data, including a Process child launched while CPU sampling
is disabled. A setup mapping alone does not prove later lifetime coverage.
[Linux v7.0 evsel.c, `evsel__config`](https://github.com/torvalds/linux/blob/v7.0/tools/perf/util/evsel.c)

The implicit `dummy:u` sets `no_aux_samples=true`, and `evsel__config`
excludes that event from global call-graph configuration. Thus an explicit
DWARF call-graph option on the CPU event does not add DWARF stack samples
to this auxiliary tracking event. Retain the actual event attributes rather
than counting tracking records as CPU samples.
[Linux v7.0 evlist.c, `evlist__add_aux_dummy`](https://github.com/torvalds/linux/blob/v7.0/tools/perf/util/evlist.c),
[evsel.c, `evsel__config`](https://github.com/torvalds/linux/blob/v7.0/tools/perf/util/evsel.c)

The earlier upstream v6.1 implementation uses `record__open` and a plain
dummy for initial-delay tracking; named/global control and acknowledgment
semantics are the same. This comparison does not qualify v6.1 on the current
host.
[Linux v6.1 builtin-record.c](https://github.com/torvalds/linux/blob/v6.1/tools/perf/builtin-record.c),
[evlist.c](https://github.com/torvalds/linux/blob/v6.1/tools/perf/util/evlist.c)

## Control and acknowledgment limits

Bare `disable` calls the all-event disable path, including the dummy.
Named `disable cpu-clock:u` targets the sampling event and can leave tracking
enabled. Named lookup failure still reaches the acknowledgment path; an ack
therefore does not independently prove successful event selection. Check the
retained event list and stderr.

The acknowledgment follows command dispatch, carries no command ID or
effective host timestamp, and is not a ring-buffer drain. In this version
`sizeof("ack\n")` is written: readers must tolerate the trailing NUL byte.
Serialize outstanding commands to associate acknowledgments.
[Linux v7.0 evlist.c, `evlist__ctlfd_enable`, `evlist__ctlfd_process` and `evlist__ctlfd_ack`](https://github.com/torvalds/linux/blob/v7.0/tools/perf/util/evlist.c),
[command tags in evlist.h](https://github.com/torvalds/linux/blob/v7.0/tools/perf/util/evlist.h)

The kernel's sideband iteration skips events below the inactive state, so
disabling the tracking event can suppress subsequent metadata. Keep the dummy
enabled during a windowed experiment, including after its CPU sampling window.
[Linux v7.0 kernel/events/core.c, `perf_iterate_ctx` and `perf_iterate_sb`](https://github.com/torvalds/linux/blob/v7.0/kernel/events/core.c)

## Truthful experimental interpretation

Record requested Virtual-time/Task/index selection independently from host
observation, command-write, acknowledgment and first/last perf-sample host timestamps. The
controller can observe only its own timestamps around a command and ack;
effective collection transitions remain bounded observations rather than exact
Activation edges. The scheduler must not wait for acknowledgments. This is an
inference from the asynchronous control path above, not a measured latency
bound. A short selected Activation can finish before enable is processed.

`PERF_SAMPLE_TIME` records host timestamps of CPU samples. Lost and throttle records are
separate event types; deliberately disabled sampling must therefore be a
distinct coverage state. Neither a zero-sample window nor an ack proves absence
of loss. Pausing output instead discards generated samples and can create lost
records, so it is not equivalent to disabling sampling.
[perf_event_open(2), sample and record formats and `PERF_EVENT_IOC_PAUSE_OUTPUT`](https://man7.org/linux/man-pages/man2/perf_event_open.2.html)

The experiment must separately retain empty/unreached selection, failed or
unclosed work, observation loss, control failure, capture-limit and storage-
reserve stops. No source finding here establishes reduced file volume,
acceptable perturbation, exact short-Activation isolation, or a production
coverage/schema contract; those require the named Linux experiment.
