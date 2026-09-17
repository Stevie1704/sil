# SiL Framework — build / test / run entry points.
# Mirrors the commands in README.md and .github/workflows/ci.yml.

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
