# SiL Framework — build / test / run entry points.
# Mirrors the commands in README.md and .github/workflows/ci.yml.

# Config -----------------------------------------------------------------------
BUILD_DIR    ?= build
BUILD_TYPE   ?= Release
PYTHON       := .venv/bin/python
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
	PYTHONPATH=python/src $(PYTHON) -m sil.check $(ARGS) --runner ./$(BUILD_DIR)/sil-run

# Housekeeping -----------------------------------------------------------------
.PHONY: clean
clean: ## Remove the build directory and caches
	rm -rf $(BUILD_DIR) .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

.PHONY: distclean
distclean: clean ## Also remove the Python virtualenv
	rm -rf .venv
