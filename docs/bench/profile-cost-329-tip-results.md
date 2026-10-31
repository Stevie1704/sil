# Profile command cost: 329-tip

- Machine: Apple M1 Pro, macOS, arm64
- Revision: `67fee44a2cec7cb84c564342ae06bb6e97d4f2c1`
- Tools: fake perf. The fake perf of tests/fake_perf.py does not sample. Its capture and its export are small, so post-Run processing here is far below that of perf.
- Load: One coding agent, no other build or test. Desktop session open. Load averages at start: { 3,28 5,35 9,00 }
- Repetitions: 9
- Runner end: Modification time of the side-car, the last file of the runner. It is on the same clock as the start of the command, with file-system resolution. The end of perf or Heaptrack is in post-Run time.

Values in ms: min / median / max.

| Case | Mode | End to end | Capture and Run | Post-Run | Profile hash | Profile hash ops | Profile hash MiB | Runner input digests | Runner Recording |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cpu | full | 658.5 / 666.9 / 677.1 | 453.0 / 456.1 / 464.8 | 202.4 / 209.1 / 224.1 | 36.9 / 37.2 / 38.9 | 4 | 72.4 | 2 | calculated |
| cpu | external | 602.2 / 609.1 / 624.1 | 402.9 / 407.5 / 413.2 | 195.3 / 202.1 / 216.6 | 36.8 / 37.0 / 37.6 | 4 | 72.4 | 0 | omitted |
| cpu-activations | full | 4015.1 / 4038.1 / 4104.4 | 3039.6 / 3044.6 / 3082.7 | 973.9 / 993.5 / 1021.7 | 67.9 / 68.3 / 68.6 | 5 | 136.4 | 2 | calculated |
| cpu-activations | external | 3971.2 / 3998.5 / 4057.2 | 2989.8 / 2999.1 / 3029.9 | 975.5 / 996.7 / 1027.2 | 68.0 / 68.3 / 68.7 | 5 | 136.4 | 0 | omitted |
