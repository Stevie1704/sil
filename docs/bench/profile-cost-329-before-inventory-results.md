# Profile command cost: 329-before-inventory

- Machine: Apple M1 Pro, macOS, arm64
- Revision: `693594bcc48a8001046a0c489f37fcab1dc1c429`
- Tools: fake perf. The fake perf of tests/fake_perf.py does not sample. Its capture and its export are small, so post-Run processing here is far below that of perf.
- Load: One coding agent, no other build or test. Desktop session open. Load averages at start: { 6,96 6,50 9,83 }
- Repetitions: 9
- Runner end: Modification time of the side-car, the last file of the runner. It is on the same clock as the start of the command, with file-system resolution. The end of perf or Heaptrack is in post-Run time.

Values in ms: min / median / max.

| Case | Mode | End to end | Capture and Run | Post-Run | Profile hash | Profile hash ops | Profile hash MiB | Runner input digests | Runner Recording |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cpu | full | 661.2 / 668.3 / 692.6 | 452.5 / 459.3 / 470.0 | 205.1 / 211.1 / 222.6 | 37.0 / 37.1 / 37.3 | 4 | 72.4 | 2 | calculated |
| cpu | external | 597.9 / 619.5 / 638.1 | 404.6 / 407.6 / 413.0 | 190.5 / 207.9 / 225.1 | 36.8 / 37.1 / 37.3 | 4 | 72.4 | 0 | omitted |
| cpu-activations | full | 4086.1 / 4117.2 / 4204.9 | 3045.5 / 3059.0 / 3092.3 | 1040.6 / 1051.9 / 1112.6 | 136.7 / 137.0 / 140.8 | 10 | 272.8 | 2 | calculated |
| cpu-activations | external | 4056.9 / 4061.6 / 4088.2 | 3005.0 / 3007.0 / 3011.1 | 1049.4 / 1056.6 / 1081.4 | 136.7 / 137.0 / 138.4 | 10 | 272.8 | 0 | omitted |
