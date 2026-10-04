# SiL Framework — build / test / run entry points.
# Mirrors the commands in CONTRIBUTING.md and .github/workflows/ci.yml.

# Config -----------------------------------------------------------------------
BUILD_DIR    ?= build
BUILD_TYPE   ?= Release
PYTHON       := .venv/bin/python
PYTHON_BIN   := $(CURDIR)/.venv/bin
# Absolute, because `sil-run` starts each participant in its own kernel-owned
# working directory: a relative entry would be resolved from there, not from
# the source tree. See docs/step-protocol.md.
SRC          := $(CURDIR)/python/src
JOBS         ?=

# Args passed through to `make run` / `make check`, e.g.
#   make run ARGS="manifest.json -o out.mcap"
ARGS         ?=

.DEFAULT_GOAL := help

# Help -------------------------------------------------------------------------
.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# Environment ------------------------------------------------------------------
.PHONY: venv
venv: $(PYTHON) ## Create the venv and install the Python package (with dev deps)
$(PYTHON):
	uv venv .venv
	uv pip install -p $(PYTHON) -e "python/[dev]"

# Build ------------------------------------------------------------------------
$(BUILD_DIR)/CMakeCache.txt:
	cmake -S . -B $(BUILD_DIR) -DCMAKE_BUILD_TYPE=$(BUILD_TYPE)

.PHONY: configure
configure: $(BUILD_DIR)/CMakeCache.txt ## Configure the CMake build directory

.PHONY: build
build: configure ## Build the kernel (sil-run)
	cmake --build $(BUILD_DIR) -j $(JOBS)

# Test -------------------------------------------------------------------------
# The pytest suite (via conftest.py) builds the kernel itself; SIL_SKIP_BUILD=1
# skips that once `make build` has run, matching CI.
.PHONY: test
test: venv build ## Run the full pytest suite (incl. determinism exit criterion)
	ctest --test-dir $(BUILD_DIR) --output-on-failure
	SIL_SKIP_BUILD=1 $(PYTHON) -m pytest tests/ -v

.PHONY: test-fast
test-fast: venv ## Run tests, letting pytest build the kernel on demand
	$(PYTHON) -m pytest tests/

# Run --------------------------------------------------------------------------
.PHONY: run
run: build ## Run a manifest: make run ARGS="manifest.json -o out.mcap"
	./$(BUILD_DIR)/sil-run $(ARGS)

.PHONY: check
check: venv build ## Determinism check: make check ARGS="manifest.json"
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.check $(ARGS) --runner ./$(BUILD_DIR)/sil-run

# Static: reads declared capacities and slot counts, runs nothing. This is the
# evidence #55's peak-memory gate asks for.
.PHONY: footprint
footprint: venv ## Declared payload memory: make footprint ARGS="manifest.json"
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.footprint $(ARGS)

# Example ----------------------------------------------------------------------
# Convenience over `make run`: builds the ACC reference example's nominal
# Manifest and runs it. The Manifest deliberately names `python3` so source
# and wheel builds have identical bytes; put this venv first on PATH so the
# child participants use the declared Python environment. `sil-run` spawns the
# participants as child processes, so PYTHONPATH has to be set on the run as
# well as on the build — absolute, because each child starts in its own
# kernel-owned working directory.
.PHONY: example
example: venv build ## Run the ACC example and record it into the build directory
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) $(PYTHON) -m sil.examples.acc.manifest $(BUILD_DIR)/acc.json
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/acc.json -o $(BUILD_DIR)/acc.mcap

# Convenience over `make run` for the FMU import example. The importer extracts
# the archive into the participant's kernel-owned working directory, which the
# kernel removes after the Run, so this leaves the working tree clean.
.PHONY: example-fmu
example-fmu: venv build ## Run the FMU example and record it into the build directory
	PYTHONPATH=$(SRC) $(PYTHON) examples/fmu/manifest.py $(BUILD_DIR)/fmu.json
	PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/fmu.json -o $(BUILD_DIR)/fmu.mcap

# Convenience over `make run` for the CSV replay example: convert the CSV into
# a Recording and a receipt twice, build the Manifest that replays it into the
# observer, and run it twice. `cmp` fails the target when either the two
# conversions or the two Run Recordings differ.
.PHONY: example-csv
example-csv: venv build ## Convert the example CSV, replay it twice, compare
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.csv_recording examples/csv/mapping.json examples/csv/signals.csv -o $(BUILD_DIR)/signals.mcap --receipt $(BUILD_DIR)/signals.receipt.json
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.csv_recording examples/csv/mapping.json examples/csv/signals.csv -o $(BUILD_DIR)/signals-2.mcap --receipt $(BUILD_DIR)/signals-2.receipt.json
	cmp $(BUILD_DIR)/signals.mcap $(BUILD_DIR)/signals-2.mcap
	PYTHONPATH=$(SRC) $(PYTHON) examples/csv/manifest.py $(BUILD_DIR)/csv-replay.json --recording $(BUILD_DIR)/signals.mcap
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/csv-replay.json -o $(BUILD_DIR)/csv-replay-1.mcap
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/csv-replay.json -o $(BUILD_DIR)/csv-replay-2.mcap
	cmp $(BUILD_DIR)/csv-replay-1.mcap $(BUILD_DIR)/csv-replay-2.mcap

# Convenience over `make run` for the recorded-data FMU example: build and
# package the example FMU, convert the recorded input and the independent
# reference, author the Manifest twice, run it twice and compare the Run with
# the reference. `cmp` fails the target when the two Manifests or the two Run
# Recordings differ, and `sil.compare` when the outputs leave the contract.
.PHONY: example-fmu-replay
example-fmu-replay: venv build ## Author and replay the recorded-data FMU example twice, compare
	PYTHONPATH=$(SRC) $(PYTHON) examples/fmu-replay/package.py $(BUILD_DIR)/EgoMotion.so -o $(BUILD_DIR)/EgoMotion.fmu
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.csv_recording examples/fmu-replay/mapping.json examples/fmu-replay/recorded.csv -o $(BUILD_DIR)/fmu-recorded.mcap --receipt $(BUILD_DIR)/fmu-recorded.receipt.json
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.csv_recording examples/fmu-replay/reference-mapping.json examples/fmu-replay/reference.csv -o $(BUILD_DIR)/fmu-reference.mcap --receipt $(BUILD_DIR)/fmu-reference.receipt.json
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.fmi.authoring examples/fmu-replay/authoring.json $(BUILD_DIR)/EgoMotion.fmu --recording $(BUILD_DIR)/fmu-recorded.mcap -o $(BUILD_DIR)/fmu-replay.json --receipt $(BUILD_DIR)/fmu-replay.receipt.json
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.fmi.authoring examples/fmu-replay/authoring.json $(BUILD_DIR)/EgoMotion.fmu --recording $(BUILD_DIR)/fmu-recorded.mcap -o $(BUILD_DIR)/fmu-replay-2.json --receipt $(BUILD_DIR)/fmu-replay-2.receipt.json
	cmp $(BUILD_DIR)/fmu-replay.json $(BUILD_DIR)/fmu-replay-2.json
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/fmu-replay.json -o $(BUILD_DIR)/fmu-replay-1.mcap
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/fmu-replay.json -o $(BUILD_DIR)/fmu-replay-2.mcap
	cmp $(BUILD_DIR)/fmu-replay-1.mcap $(BUILD_DIR)/fmu-replay-2.mcap
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.compare examples/fmu-replay/contract.json $(BUILD_DIR)/fmu-replay-1.mcap $(BUILD_DIR)/fmu-reference.mcap

# The same sequence for the Float32, Int32, UInt32 and UInt64 bindings: the
# Reference FMU `Feedthrough` takes the recorded values and hands them back,
# and the comparison is exact against the hand-written reference.
NUMERIC_FMU := tests/fixtures/reference-fmus/3.0/Feedthrough.fmu
.PHONY: example-fmu-numeric
example-fmu-numeric: venv build ## Replay Float32/Int32/UInt32/UInt64 into Feedthrough twice, compare
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.csv_recording examples/fmu-numeric/mapping.json examples/fmu-numeric/recorded.csv -o $(BUILD_DIR)/fmu-numeric-recorded.mcap
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.csv_recording examples/fmu-numeric/reference-mapping.json examples/fmu-numeric/reference.csv -o $(BUILD_DIR)/fmu-numeric-reference.mcap
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.fmi.authoring examples/fmu-numeric/authoring.json $(NUMERIC_FMU) --recording $(BUILD_DIR)/fmu-numeric-recorded.mcap -o $(BUILD_DIR)/fmu-numeric.json --receipt $(BUILD_DIR)/fmu-numeric.receipt.json
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/fmu-numeric.json -o $(BUILD_DIR)/fmu-numeric-1.mcap
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/fmu-numeric.json -o $(BUILD_DIR)/fmu-numeric-2.mcap
	cmp $(BUILD_DIR)/fmu-numeric-1.mcap $(BUILD_DIR)/fmu-numeric-2.mcap
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.compare examples/fmu-numeric/contract.json $(BUILD_DIR)/fmu-numeric-1.mcap $(BUILD_DIR)/fmu-numeric-reference.mcap

# Convenience over `make run` for the coupled-FMU example: author the delayed
# feedback loop between two Feedthrough instances twice, run it twice, and
# `cmp` both pairs. The authoring command prints the plan of the Run.
.PHONY: example-fmu-coupling
example-fmu-coupling: venv build ## Author the coupled-FMU example twice and run it twice, compare
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.fmi.coupling examples/fmu-coupling/feedback.json --fmu left tests/fixtures/reference-fmus/3.0/Feedthrough.fmu --fmu right tests/fixtures/reference-fmus/3.0/Feedthrough.fmu -o $(BUILD_DIR)/fmu-coupling.json --receipt $(BUILD_DIR)/fmu-coupling.receipt.json
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.fmi.coupling examples/fmu-coupling/feedback.json --fmu left tests/fixtures/reference-fmus/3.0/Feedthrough.fmu --fmu right tests/fixtures/reference-fmus/3.0/Feedthrough.fmu -o $(BUILD_DIR)/fmu-coupling-2.json > /dev/null
	cmp $(BUILD_DIR)/fmu-coupling.json $(BUILD_DIR)/fmu-coupling-2.json
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/fmu-coupling.json -o $(BUILD_DIR)/fmu-coupling-1.mcap
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/fmu-coupling.json -o $(BUILD_DIR)/fmu-coupling-2.mcap
	cmp $(BUILD_DIR)/fmu-coupling-1.mcap $(BUILD_DIR)/fmu-coupling-2.mcap

# Convenience over `make run` for replacing one coupled FMU with replay: run
# the feedback loop, replace `left` by its recorded Channel, run the
# replacement twice, `cmp` its Recordings and compare the retained outputs
# with the original Run's Messages.
.PHONY: example-fmu-substitution
example-fmu-substitution: example-fmu-coupling ## Replace one coupled FMU with replay and compare the retained outputs
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.fmi.substitution examples/fmu-coupling/feedback.json --fmu left tests/fixtures/reference-fmus/3.0/Feedthrough.fmu --fmu right tests/fixtures/reference-fmus/3.0/Feedthrough.fmu --replace left --recording $(BUILD_DIR)/fmu-coupling-1.mcap -o $(BUILD_DIR)/fmu-substitution.json --contract $(BUILD_DIR)/fmu-substitution.contract.json --receipt $(BUILD_DIR)/fmu-substitution.receipt.json
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/fmu-substitution.json -o $(BUILD_DIR)/fmu-substitution-1.mcap
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/fmu-substitution.json -o $(BUILD_DIR)/fmu-substitution-2.mcap
	cmp $(BUILD_DIR)/fmu-substitution-1.mcap $(BUILD_DIR)/fmu-substitution-2.mcap
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.compare $(BUILD_DIR)/fmu-substitution.contract.json $(BUILD_DIR)/fmu-substitution-1.mcap $(BUILD_DIR)/fmu-coupling-1.mcap

# Convenience over `make run` for the shared-library example: build the
# example library the way an adopter would, convert its recorded input, and
# run the Manifest that replays it into two adapter instances twice. The
# response deadline makes a hung library fail the Run instead of stopping it.
# `cmp` fails the target when the two Run Recordings differ.
.PHONY: example-library
example-library: venv build ## Replay recorded input into the example C library twice, compare
	cc -shared -fPIC -O2 -o $(BUILD_DIR)/example-speed_filter.so examples/library/speed_filter.c
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.csv_recording examples/library/mapping.json examples/library/signals.csv -o $(BUILD_DIR)/library-signals.mcap --receipt $(BUILD_DIR)/library-signals.receipt.json
	PYTHONPATH=$(SRC) $(PYTHON) examples/library/manifest.py $(BUILD_DIR)/library.json --recording $(BUILD_DIR)/library-signals.mcap --library $(BUILD_DIR)/example-speed_filter.so
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/library.json -o $(BUILD_DIR)/library-1.mcap --participant-timeout-ms 10000
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/library.json -o $(BUILD_DIR)/library-2.mcap --participant-timeout-ms 10000
	cmp $(BUILD_DIR)/library-1.mcap $(BUILD_DIR)/library-2.mcap

# The port binding of the shared-library adapter: two recorded inputs into a
# library with two outputs and two cyclic entry points, run twice. `cmp`
# fails the target when the two Run Recordings differ.
.PHONY: example-library-ports
example-library-ports: venv build ## Replay two recorded inputs into the example multi-port C library twice, compare
	cc -shared -fPIC -O2 -o $(BUILD_DIR)/example-gap_monitor.so examples/library/gap_monitor.c
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.csv_recording examples/library/gap_mapping.json examples/library/gap_signals.csv -o $(BUILD_DIR)/gap-signals.mcap --receipt $(BUILD_DIR)/gap-signals.receipt.json
	PYTHONPATH=$(SRC) $(PYTHON) examples/library/gap_manifest.py $(BUILD_DIR)/gap.json --recording $(BUILD_DIR)/gap-signals.mcap --library $(BUILD_DIR)/example-gap_monitor.so
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/gap.json -o $(BUILD_DIR)/gap-1.mcap --participant-timeout-ms 10000
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/gap.json -o $(BUILD_DIR)/gap-2.mcap --participant-timeout-ms 10000
	cmp $(BUILD_DIR)/gap-1.mcap $(BUILD_DIR)/gap-2.mcap

# The shared-library example over a selected replay window: run the library
# over the full history and over the window after its warm-up, then compare
# the two over the evaluation interval. The Durations are the window's end_ns
# and the receipt's duration_ns.
.PHONY: example-window
example-window: venv build ## Replay a window of the library history after a warm-up, compare with the full history
	cc -shared -fPIC -O2 -o $(BUILD_DIR)/example-speed_filter.so examples/library/speed_filter.c
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.csv_recording examples/library/mapping.json examples/library/history.csv -o $(BUILD_DIR)/library-history.mcap --receipt $(BUILD_DIR)/library-history.receipt.json
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.replay_window examples/library/window.json $(BUILD_DIR)/library-history.mcap -o $(BUILD_DIR)/library-window.mcap --receipt $(BUILD_DIR)/library-window.receipt.json
	PYTHONPATH=$(SRC) $(PYTHON) examples/library/manifest.py $(BUILD_DIR)/library-full.json --recording $(BUILD_DIR)/library-history.mcap --library $(BUILD_DIR)/example-speed_filter.so --duration-ns 2500000000
	PYTHONPATH=$(SRC) $(PYTHON) examples/library/manifest.py $(BUILD_DIR)/library-windowed.json --recording $(BUILD_DIR)/library-window.mcap --library $(BUILD_DIR)/example-speed_filter.so --duration-ns 2000000000
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/library-full.json -o $(BUILD_DIR)/library-full.mcap --participant-timeout-ms 10000
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/library-windowed.json -o $(BUILD_DIR)/library-windowed-1.mcap --participant-timeout-ms 10000
	PATH=$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) ./$(BUILD_DIR)/sil-run $(BUILD_DIR)/library-windowed.json -o $(BUILD_DIR)/library-windowed-2.mcap --participant-timeout-ms 10000
	cmp $(BUILD_DIR)/library-windowed-1.mcap $(BUILD_DIR)/library-windowed-2.mcap
	PYTHONPATH=$(SRC) $(PYTHON) examples/library/window_contract.py $(BUILD_DIR)/library-window.receipt.json -o $(BUILD_DIR)/library-window.contract.json
	PYTHONPATH=$(SRC) $(PYTHON) -m sil.compare $(BUILD_DIR)/library-window.contract.json $(BUILD_DIR)/library-windowed-1.mcap $(BUILD_DIR)/library-full.mcap

# The Native ADAS reference application over installed interfaces: stage the
# build into a prefix, then run examples/adas-reference/run.sh with only that
# prefix and this venv on PATH. The script builds the library against the
# staged include/sil and silschema, runs the Manifest twice and `cmp`s the
# Recordings, compares every maneuver with its enumerated trajectory, and
# requires the wrong-sign build to fail.
.PHONY: example-adas-reference
example-adas-reference: venv build ## Build the C ADAS reference against the staged install, run twice, compare
	cmake --install $(BUILD_DIR) --prefix $(BUILD_DIR)/adas-reference-prefix
	PATH=$(CURDIR)/$(BUILD_DIR)/adas-reference-prefix/bin:$(PYTHON_BIN):$$PATH PYTHONPATH=$(SRC) examples/adas-reference/run.sh $(BUILD_DIR)/adas-reference

# Benchmark --------------------------------------------------------------------
# Regenerates the routing baseline in docs/bench/ (issue #61). Long-running:
# every row is run once instrumented for copy counts and several times
# uninstrumented for wall-clock.
.PHONY: bench
bench: venv build ## Regenerate the routing baseline: make bench ARGS="--repeats 3"
	$(PYTHON) tools/bench_routing.py --build-dir $(BUILD_DIR) $(ARGS)

# Housekeeping -----------------------------------------------------------------
.PHONY: clean
clean: ## Remove the build directory and caches
	rm -rf $(BUILD_DIR) .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

.PHONY: distclean
distclean: clean ## Also remove the Python virtualenv
	rm -rf .venv
