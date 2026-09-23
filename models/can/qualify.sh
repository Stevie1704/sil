#!/usr/bin/env bash
set -euo pipefail
ulimit -c 0
export PYTHONPATH=/work/python/src
python models/can/build.py build/can/SilCanSmoke.fmu
python models/can/build.py build/can/repeat.fmu
cmp build/can/SilCanSmoke.fmu build/can/repeat.fmu
python models/can/qualification/build_nodes.py /opt/examples /opt/spec build/can
c++ -std=c++20 -O1 -g -fsanitize=undefined -fno-sanitize-recover=all -fno-omit-frame-pointer \
    -Imodels/can/src models/can/src/bus.cpp models/can/tests/core.cpp -o build/can/core-test
build/can/core-test
python -m pytest models/can/tests -v --junitxml=build/can/results.xml
python models/can/qualification/report.py
