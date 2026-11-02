# Profile command cost: 329-base

- Machine: Apple M1 Pro, macOS, arm64
- Revision: `600fa429dfcca50b11eb5db9b6735d6d52bcea89`
- Tools: fake perf. The fake perf of tests/fake_perf.py does not sample. Its capture and its export are small, so post-Run processing here is far below that of perf.
- Load: One coding agent, no other build or test. Desktop session open. Load averages at start: { 5,30 5,95 9,86 }
- Repetitions: 9
- Runner end: Modification time of the side-car, the last file of the runner. It is on the same clock as the start of the command, with file-system resolution. The end of perf or Heaptrack is in post-Run time.

Values in ms: min / median / max.

| Case | Mode | End to end | Capture and Run | Post-Run | Profile hash | Profile hash ops | Profile hash MiB | Runner input digests | Runner Recording |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cpu | default | 994.6 / 1010.7 / 1041.8 | 789.7 / 793.6 / 831.7 | 204.9 / 213.6 / 230.0 | 36.9 / 37.2 / 38.1 | 4 | 72.4 | 2 | calculated |
| cpu-activations | default | 4437.5 / 4475.9 / 4525.9 | 3374.2 / 3391.4 / 3457.3 | 1058.0 / 1071.9 / 1097.2 | 137.1 / 137.7 / 139.6 | 10 | 272.8 | 2 | calculated |
