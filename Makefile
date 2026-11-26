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

# Arguments for make generate, e.g. GENERATE_ARGS="--provider stripe --count 5".
GENERATE_ARGS ?=

# Extra arguments for sam build, e.g. make build SAM_BUILD_ARGS=--use-container.
SAM_BUILD_ARGS ?=
# Design note section 13: one region for everything.
REGION := eu-west-2
# SAM CLI sends usage metrics unless told not to: off for every recipe, here and in CI.
export SAM_CLI_TELEMETRY := 0


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
			if (description != "") printf "  make %-14s %s\n", target[1], description; \
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
## Run the tests; coverage goes to the terminal, coverage.xml and htmlcov/ (PYTEST_ARGS="-k name" narrows them).
test:
	$(RUN) pytest --cov=webhook_gateway --cov-report=term-missing --cov-report=xml --cov-report=html $(PYTEST_ARGS)

.PHONY: check
## Run lint, typecheck and test; stops at the first that fails.
check: lint typecheck test

.PHONY: hooks
## Install the git hooks (ruff, mypy, file checks, gitleaks); once per clone.
hooks:
	$(RUN) pre-commit install --install-hooks

.PHONY: scan-secrets
## Scan every commit in the git history for secrets with gitleaks.
scan-secrets:
	$(RUN) pre-commit run gitleaks-history --hook-stage manual --verbose

.PHONY: schema
## Regenerate schemas/envelope.v1.json from the Envelope model; commit the result.
schema:
	$(RUN) python -m webhook_gateway.common.write_schema schemas/envelope.v1.json

.PHONY: generate
## Print signed synthetic webhooks as JSON Lines; needs SIGNING_SECRET (GENERATE_ARGS="--provider stripe").
generate:
	@$(RUN) python -m webhook_gateway.synthetic.cli $(GENERATE_ARGS)

.PHONY: validate
## Check template.yaml with SAM's translator and cfn-lint; needs no AWS account.
validate:
	sam validate --lint --region $(REGION)

.PHONY: build
## Bundle each function into .aws-sam/ (needs python3.14 on PATH, or SAM_BUILD_ARGS=--use-container).
build:
	poetry export --only main --format requirements.txt --output src/requirements.txt
	sam build $(SAM_BUILD_ARGS)
