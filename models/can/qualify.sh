#!/usr/bin/env bash
set -euo pipefail
ulimit -c 0
export PYTHONPATH=/work/python/src
python models/can/build.py build/can/SilCanBus.fmu
python models/can/build.py build/can/repeat.fmu
# Refuses to stage anything if the two controlled builds differ by one byte.
python models/can/release.py build/can/SilCanBus.fmu build/can/repeat.fmu \
    build/can/release build/can/image.txt
python models/can/qualification/build_nodes.py /opt/examples /opt/spec build/can
# ASan cannot start under qemu emulation; an emulated host may select
# CAN_SANITIZERS=undefined. CI runs natively on x86-64 with the default.
sanitize=(-O1 -g "-fsanitize=${CAN_SANITIZERS:-address,undefined}" -fno-sanitize-recover=all
          -fno-omit-frame-pointer)
echo "native sanitizers: ${CAN_SANITIZERS:-address,undefined}" > build/can/sanitizers.txt
c++ -std=c++20 "${sanitize[@]}" \
    -Imodels/can/src models/can/src/bus.cpp models/can/tests/core.cpp -o build/can/core-test
build/can/core-test
# The exported C entry points, compiled from the same prepared sources as the FMU.
rm -rf build/can/native-src
python models/can/build.py --sources build/can/native-src
native=(build/can/native-src/bus.cpp build/can/native-src/fmi.cpp build/can/native-src/unsupported.cpp)
c++ -std=c++20 "${sanitize[@]}" -Ibuild/can/native-src "${native[@]}" \
    models/can/tests/abi.cpp -o build/can/abi-test
build/can/abi-test build/can/abi.json
c++ -std=c++20 -O2 -Ibuild/can/native-src "${native[@]}" \
    models/can/tests/capacity.cpp -o build/can/capacity
build/can/capacity build/can/capacity.json
python -m pytest models/can/tests -v --junitxml=build/can/results.xml
python models/can/qualification/report.py
