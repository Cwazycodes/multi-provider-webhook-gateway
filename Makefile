# The single entry point: everything in this repo runs through a make target,
# and `make help` lists them. A ticket that adds a command adds a target here
# (with a ## line), not a step in the README.

ifneq ($(filter 3.%,$(MAKE_VERSION)),)
$(error GNU Make 4+ is required, this is $(MAKE_VERSION); on macOS: brew install make, then see the README)
endif

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
MAKEFLAGS += --warn-undefined-variables --no-builtin-rules
.DEFAULT_GOAL := help

RUN := poetry run
# Extra pytest arguments, e.g. make test PYTEST_ARGS="-k makefile".
PYTEST_ARGS ?=

# The first line of every recipe that talks to AWS. STAGE and AWS_PROFILE must
# be given on the make command line and not be empty. Values exported in your
# shell don't count, so a leftover `export AWS_PROFILE=...` can't aim a command.
require-aws = $(foreach v,STAGE AWS_PROFILE,$(if $(and $(filter command line,$(origin $v)),$($v)),,$(error make $@ talks to AWS: pass $v explicitly, e.g. make $@ STAGE=dev AWS_PROFILE=<profile>)))

.PHONY: help
## List every target (the default when you run plain `make`).
help:
	@awk '/^## / { description = substr($$0, 4); next } \
		/^[a-zA-Z0-9_-]+:/ { \
			split($$0, target, ":"); \
			if (description != "") printf "  make %-10s %s\n", target[1], description; \
			description = "" \
		}' $(MAKEFILE_LIST)
.PHONY: install
## Install exactly what poetry.lock lists, plus the project itself.
install:
	poetry sync

.PHONY: fmt
## Sort imports and format the code with Ruff.
fmt:
	$(RUN) ruff check --select I --fix .
	$(RUN) ruff format .

.PHONY: lint
## Lint with Ruff, and fail on code that make fmt would change.
lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

.PHONY: typecheck
## Type-check src/ and tests/ with strict mypy.
typecheck:
	$(RUN) mypy

.PHONY: test
## Run the tests with a coverage report (PYTEST_ARGS="-k name" narrows them).
test:
	$(RUN) pytest --cov=webhook_gateway --cov-report=term-missing $(PYTEST_ARGS)

.PHONY: check
## Run lint, typecheck and test; stops at the first that fails.
check: lint typecheck test
