# Profiling qualification: offline-render

perf: none; failed checks: 0

| Scenario | Exit | Wall ms | Checks | Failed |
| --- | --- | --- | --- | --- |
| render-container-cap-perfmon-cpu-activation-limit | 0 | 194 | 8 | - |
| render-container-cap-perfmon-cpu-capture-limit | 0 | 173 | 8 | - |
| render-container-cap-perfmon-cpu-main-matching-activations | 0 | 207 | 8 | - |
| render-container-cap-perfmon-cpu-mixed-activations | 0 | 248 | 8 | - |
| render-container-cap-perfmon-heap-capture-limit | 0 | 91 | 8 | - |
| render-container-cap-perfmon-heap-mixed-group | 0 | 1357 | 8 | - |
| render-container-cap-perfmon-heap-runner-debug-matching | 0 | 113 | 8 | - |
| render-native-paranoid-2-cpu-activation-limit | 0 | 146 | 8 | - |
| render-native-paranoid-2-cpu-capture-limit | 0 | 126 | 8 | - |
| render-native-paranoid-2-cpu-main-matching-activations | 0 | 156 | 8 | - |
| render-native-paranoid-2-cpu-mixed-activations | 0 | 156 | 8 | - |
| render-native-paranoid-default-heap-capture-limit | 0 | 88 | 8 | - |
| render-native-paranoid-default-heap-mixed-group | 0 | 407 | 8 | - |
| render-native-paranoid-default-heap-runner-debug-matching | 0 | 110 | 8 | - |

## Observations

- render-container-cap-perfmon-cpu-activation-limit: {"html_bytes": 499773, "set_bytes": {"activation/correlation.json": 134864, "cpu/samples.json": 359865, "profile.json": 77059}, "wall_ms": 194}
- render-container-cap-perfmon-cpu-capture-limit: {"html_bytes": 580206, "set_bytes": {"cpu/samples.json": 123521, "profile.json": 42344}, "wall_ms": 173}
- render-container-cap-perfmon-cpu-main-matching-activations: {"html_bytes": 642044, "set_bytes": {"activation/correlation.json": 149952, "cpu/samples.json": 336262, "profile.json": 75093}, "wall_ms": 207}
- render-container-cap-perfmon-cpu-mixed-activations: {"html_bytes": 875278, "set_bytes": {"activation/correlation.json": 135788, "cpu/samples.json": 477437, "profile.json": 81727}, "wall_ms": 248}
- render-container-cap-perfmon-heap-capture-limit: {"html_bytes": 68542, "set_bytes": {"heap/allocations.json": 47771, "profile.json": 7889}, "wall_ms": 91}
- render-container-cap-perfmon-heap-mixed-group: {"html_bytes": 178798, "set_bytes": {"heap/allocations.json": 6050532, "profile.json": 23802}, "wall_ms": 1357}
- render-container-cap-perfmon-heap-runner-debug-matching: {"html_bytes": 80503, "set_bytes": {"heap/allocations.json": 175491, "profile.json": 11521}, "wall_ms": 113}
- render-native-paranoid-2-cpu-activation-limit: {"html_bytes": 291370, "set_bytes": {"activation/correlation.json": 115851, "cpu/samples.json": 189115, "profile.json": 67430}, "wall_ms": 146}
- render-native-paranoid-2-cpu-capture-limit: {"html_bytes": 355790, "set_bytes": {"cpu/samples.json": 79998, "profile.json": 56343}, "wall_ms": 126}
- render-native-paranoid-2-cpu-main-matching-activations: {"html_bytes": 446883, "set_bytes": {"activation/correlation.json": 132730, "cpu/samples.json": 179551, "profile.json": 66294}, "wall_ms": 156}
- render-native-paranoid-2-cpu-mixed-activations: {"html_bytes": 510987, "set_bytes": {"activation/correlation.json": 100783, "cpu/samples.json": 195144, "profile.json": 71510}, "wall_ms": 156}
- render-native-paranoid-default-heap-capture-limit: {"html_bytes": 68623, "set_bytes": {"heap/allocations.json": 47596, "profile.json": 8110}, "wall_ms": 88}
- render-native-paranoid-default-heap-mixed-group: {"html_bytes": 157278, "set_bytes": {"heap/allocations.json": 1491491, "profile.json": 16945}, "wall_ms": 407}
- render-native-paranoid-default-heap-runner-debug-matching: {"html_bytes": 82383, "set_bytes": {"heap/allocations.json": 176867, "profile.json": 11815}, "wall_ms": 110}
