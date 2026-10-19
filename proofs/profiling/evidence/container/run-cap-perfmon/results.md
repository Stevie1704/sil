# Profiling proof results

| Host fact | Value |
| --- | --- |
| kernel | 6.17.0-1022-azure |
| machine | x86_64 |
| libc | glibc 2.36 |
| in container | True |
| uid | 0 |
| perf_event_paranoid | 4 |
| CapEff | 00000040a80425fb |
| Seccomp | 2 |
| NSpid | 7 |
| perf | perf version 6.1.176 |
| heaptrack | heaptrack 1.4.0 |
| heaptrack_preload | /usr/lib/heaptrack/libheaptrack_preload.so |
| heaptrack_interpret | /usr/lib/heaptrack/libexec/heaptrack_interpret |
| cc | cc (Debian 12.2.0-14+deb12u1) 12.2.0 |
| objcopy | GNU objcopy (GNU Binutils for Debian) 2.40 |
| sil-run | 0.2.1 |

## Probes

| Probe | Outcome | Exit |
| --- | --- | --- |
| perf_user | available | 0 |
| perf_user_dwarf | available | 0 |
| perf_kernel | available | 0 |
| heaptrack_preload | available | 0 |

## Runs (observations, not gates)

| Run | Exit | Wall ms | Recording bytes | Collector bytes |
| --- | --- | --- | --- | --- |
| ordinary | 0 | 2736 | 28039 | None |
| perf-dwarf | 0 | 3022 | 28039 | 22870760 |
| perf-fp | 0 | 3018 | 28039 | 264084 |
| heaptrack-runner | 0 | 2721 | 28039 | runner 358053 |
| heaptrack-runner-mismatched | 0 | 2742 | 28039 | runner 357884 |
| heaptrack-fmu | 0 | 2874 | 28039 | fmu 1032568 |
| heaptrack-shimmed-process | 0 | 5122 | 28039 | process 48566508 |
| heaptrack-shimmed-process-sleep-immediate | 0 | 4383 | 28039 | process 42893303, fmu_late 1034561 |

## Checks and findings

| Kind | Result | Name |
| --- | --- | --- |
| check | pass | ordinary: Run exits 0 |
| check | pass | ordinary: no process is left |
| check | pass | ordinary: one interval per hot call |
| check | pass | perf-dwarf: Run exits 0 |
| check | pass | perf-dwarf: no process is left |
| check | pass | perf-dwarf: Recording equals the ordinary Recording |
| check | pass | perf-dwarf: samples name silproof_native_spin_even (matching debug file) |
| check | pass | perf-dwarf: samples name silproof_native_spin_odd (matching debug file) |
| check | pass | perf-dwarf: samples name silproof_process_spin (matching debug file) |
| check | pass | perf-dwarf: samples name silproof_child_spin (matching debug file) |
| check | pass | perf-dwarf: samples name silproof_fmu_spin (matching debug file) |
| check | pass | perf-dwarf: the matching debug file gives source lines |
| check | pass | perf-dwarf: a mismatched debug file gives no source line |
| check | pass | perf-dwarf: a mismatched debug file names no stripped function |
| finding | does not hold | perf-dwarf: the stripped library alone names its static functions |
| check | pass | perf-dwarf: every silproof_child_spin sample lies in one of its own host intervals |
| check | pass | perf-dwarf: every silproof_fmu_spin sample lies in one of its own host intervals |
| check | pass | perf-dwarf: every silproof_native_spin_even sample lies in one of its own host intervals |
| check | pass | perf-dwarf: every silproof_native_spin_odd sample lies in one of its own host intervals |
| check | pass | perf-dwarf: every silproof_process_spin sample lies in one of its own host intervals |
| check | pass | perf-dwarf: every workload process has samples under its own PID |
| check | pass | perf-dwarf: perf recorded the native build ID |
| check | pass | perf-dwarf: perf recorded the process build ID |
| check | pass | perf-dwarf: perf recorded the child build ID |
| check | pass | perf-dwarf: perf recorded the fmu build ID |
| finding | does not hold | perf-dwarf: the FMU library resolves after the Run directory is gone, with no explicit capture |
| check | pass | perf-dwarf: the FMU library resolves after an explicit build-ID cache capture |
| check | pass | perf-dwarf: silproof_native_spin_even samples unwind past their module |
| check | pass | perf-dwarf: silproof_native_spin_even samples show its source caller native_task |
| finding | holds | perf-dwarf: every silproof_native_spin_even sample shows its source caller |
| check | pass | perf-dwarf: silproof_native_spin_odd samples unwind past their module |
| check | pass | perf-dwarf: silproof_native_spin_odd samples show its source caller native_task |
| finding | holds | perf-dwarf: every silproof_native_spin_odd sample shows its source caller |
| check | pass | perf-dwarf: silproof_process_spin samples unwind past their module |
| check | pass | perf-dwarf: silproof_process_spin samples show its source caller silproof_process_step |
| finding | holds | perf-dwarf: every silproof_process_spin sample shows its source caller |
| check | pass | perf-dwarf: silproof_child_spin samples unwind past their module |
| check | pass | perf-dwarf: silproof_child_spin samples show its source caller main |
| finding | holds | perf-dwarf: every silproof_child_spin sample shows its source caller |
| check | pass | perf-dwarf: silproof_fmu_spin samples unwind past their module |
| check | pass | perf-dwarf: silproof_fmu_spin samples show its source caller fmi3DoStep |
| finding | holds | perf-dwarf: every silproof_fmu_spin sample shows its source caller |
| check | pass | perf-fp: Run exits 0 |
| check | pass | perf-fp: no process is left |
| check | pass | perf-fp: Recording equals the ordinary Recording |
| check | pass | perf-fp: samples name silproof_native_spin_even (matching debug file) |
| check | pass | perf-fp: samples name silproof_native_spin_odd (matching debug file) |
| check | pass | perf-fp: samples name silproof_process_spin (matching debug file) |
| check | pass | perf-fp: samples name silproof_child_spin (matching debug file) |
| finding | does not hold | perf-fp: samples name silproof_fmu_spin (matching debug file) |
| check | pass | perf-fp: the matching debug file gives source lines |
| check | pass | perf-fp: a mismatched debug file gives no source line |
| check | pass | perf-fp: a mismatched debug file names no stripped function |
| finding | does not hold | perf-fp: the stripped library alone names its static functions |
| check | pass | perf-fp: every silproof_child_spin sample lies in one of its own host intervals |
| check | pass | perf-fp: every silproof_fmu_spin sample lies in one of its own host intervals |
| check | pass | perf-fp: every silproof_native_spin_even sample lies in one of its own host intervals |
| check | pass | perf-fp: every silproof_native_spin_odd sample lies in one of its own host intervals |
| check | pass | perf-fp: every silproof_process_spin sample lies in one of its own host intervals |
| check | pass | perf-fp: every workload process has samples under its own PID |
| check | pass | perf-fp: perf recorded the native build ID |
| check | pass | perf-fp: perf recorded the process build ID |
| check | pass | perf-fp: perf recorded the child build ID |
| finding | does not hold | perf-fp: perf recorded the fmu build ID |
| finding | does not hold | perf-fp: the FMU library resolves after the Run directory is gone, with no explicit capture |
| finding | does not hold | perf-fp: the FMU library resolves after an explicit build-ID cache capture |
| finding | does not hold | perf-fp: silproof_native_spin_even samples unwind past their module |
| finding | does not hold | perf-fp: silproof_native_spin_even samples show its source caller native_task |
| finding | does not hold | perf-fp: every silproof_native_spin_even sample shows its source caller |
| finding | does not hold | perf-fp: silproof_native_spin_odd samples unwind past their module |
| finding | does not hold | perf-fp: silproof_native_spin_odd samples show its source caller native_task |
| finding | does not hold | perf-fp: every silproof_native_spin_odd sample shows its source caller |
| finding | holds | perf-fp: silproof_process_spin samples unwind past their module |
| finding | does not hold | perf-fp: silproof_process_spin samples show its source caller silproof_process_step |
| finding | does not hold | perf-fp: every silproof_process_spin sample shows its source caller |
| finding | does not hold | perf-fp: silproof_child_spin samples unwind past their module |
| finding | does not hold | perf-fp: silproof_child_spin samples show its source caller main |
| finding | does not hold | perf-fp: every silproof_child_spin sample shows its source caller |
| finding | does not hold | perf-fp: silproof_fmu_spin samples unwind past their module |
| finding | does not hold | perf-fp: silproof_fmu_spin samples show its source caller fmi3DoStep |
| finding | does not hold | perf-fp: every silproof_fmu_spin sample shows its source caller |
| check | pass | heaptrack-runner: Run exits 0 |
| check | pass | heaptrack-runner: no process is left |
| check | pass | heaptrack-runner: Recording equals the ordinary Recording |
| check | pass | heaptrack-runner: the interpreter exits 0 |
| check | pass | heaptrack-runner: the runner capture holds 1024-byte allocations from the Native library |
| check | pass | heaptrack-runner: silproof_native_alloc_site has its exact calls and size |
| check | pass | heaptrack-runner: silproof_native_retain_site has its exact calls and size |
| finding | holds | heaptrack-runner: the runner's Heaptrack timestamps can be host time |
| check | pass | heaptrack-runner: the retained block is outstanding at capture end |
| check | pass | heaptrack-runner: no Process participant allocation is in the runner capture |
| check | pass | heaptrack-runner-mismatched: Run exits 0 |
| check | pass | heaptrack-runner-mismatched: no process is left |
| check | pass | heaptrack-runner-mismatched: Recording equals the ordinary Recording |
| check | pass | heaptrack-runner-mismatched: the interpreter exits 0 |
| check | pass | heaptrack-runner-mismatched: the runner capture holds 1024-byte allocations from the Native library |
| check | pass | heaptrack-runner-mismatched: a mismatched debug file names no stripped allocation site |
| check | pass | heaptrack-fmu: Run exits 0 |
| check | pass | heaptrack-fmu: no process is left |
| check | pass | heaptrack-fmu: Recording equals the ordinary Recording |
| check | pass | heaptrack-fmu: the interpreter exits 0 |
| check | pass | heaptrack-fmu: silproof_fmu_alloc_site has its exact calls and size |
| check | pass | heaptrack-fmu: no other participant's allocation is in the fmu capture |
| finding | holds | heaptrack-fmu: the fmu participant's Heaptrack timestamps can be host time |
| finding | holds | heaptrack-shimmed-process: Run exits 0 |
| check | pass | heaptrack-shimmed-process: no process is left |
| finding | holds | heaptrack-shimmed-process: Recording equals the ordinary Recording except the child's clock reading |
| finding | does not hold | heaptrack-shimmed-process: the short-lived child keeps the Clock shim |
| finding | holds | heaptrack-shimmed-process: the interpreter exits 0 |
| finding | holds | heaptrack-shimmed-process: silproof_process_alloc_site has its exact calls and size |
| finding | holds | heaptrack-shimmed-process: no other participant's allocation is in the process capture |
| finding | does not hold | heaptrack-shimmed-process: the process participant's Heaptrack timestamps can be host time |
| finding | holds | heaptrack-shimmed-process-sleep-immediate: Run exits 0 |
| check | pass | heaptrack-shimmed-process-sleep-immediate: no process is left |
| finding | holds | heaptrack-shimmed-process-sleep-immediate: Recording equals the ordinary Recording except the child's clock reading |
| finding | does not hold | heaptrack-shimmed-process-sleep-immediate: the short-lived child keeps the Clock shim |
| finding | holds | heaptrack-shimmed-process-sleep-immediate: the interpreter exits 0 |
| finding | holds | heaptrack-shimmed-process-sleep-immediate: silproof_process_alloc_site has its exact calls and size |
| finding | holds | heaptrack-shimmed-process-sleep-immediate: no other participant's allocation is in the process capture |
| finding | does not hold | heaptrack-shimmed-process-sleep-immediate: the process participant's Heaptrack timestamps can be host time |
| finding | does not hold | heaptrack-shimmed-process-sleep-immediate: silproof_fmu_alloc_site resolves when the raw data is interpreted after the Run directory is gone |
| finding | holds | heaptrack-shimmed-process-sleep-immediate: the late fmu capture keeps every 4096-byte allocation |
