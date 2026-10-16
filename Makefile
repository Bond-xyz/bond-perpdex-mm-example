.DEFAULT_GOAL := help

ARGS ?=

.PHONY: help install setup quickstart config public private stream trade \
	signing offline discover bot-headers rest-example stream-example \
	test lint format format-check check env-template env-reference _venv

help:
	@printf '%s\n' \
	  'make quickstart       Run the configured offline maker by default' \
	  'make setup            Create/preserve configuration; ARGS=--generate-signing-key is optional' \
	  'make config           Show effective settings with secrets redacted' \
	  'make public           Read public Galileo data' \
	  'make discover         Validate public signing metadata without credentials' \
	  'make private          Read the configured private account' \
	  'make stream           Read bounded private events' \
	  'make trade ARGS=--allow-live-orders  Run one configured place/cancel' \
	  'make signing | offline | bot-headers  Run individual offline examples' \
	  'make rest-example | stream-example   Pass explicit example flags through ARGS' \
	  'make install          Install/update the local Python environment' \
	  'make check            Run tests, lint, formatting, dependency and shell checks' \
	  'Append ARGS="..." to pass flags, such as --env-file /secure/maker.env.'

install:
	@sh scripts/install.sh

setup:
	@sh scripts/setup.sh $(ARGS)

quickstart:
	@sh scripts/quickstart.sh $(ARGS)

config:
	@sh scripts/quickstart.sh --show-config $(ARGS)

public private stream trade:
	@sh scripts/quickstart.sh --mode $@ $(ARGS)

_venv:
	@if [ ! -x .venv/bin/python ]; then sh scripts/install.sh >&2; fi

signing: _venv
	@.venv/bin/python examples/01_signing_offline.py $(ARGS)

offline: _venv
	@.venv/bin/python -m bond_perpdex_client $(ARGS)

discover: _venv
	@.venv/bin/python -m bond_perpdex_client.discovery

bot-headers: _venv
	@.venv/bin/python examples/04_provisioned_key_headers.py $(ARGS)

rest-example: _venv
	@.venv/bin/python examples/02_private_rest.py $(ARGS)

stream-example: _venv
	@.venv/bin/python examples/03_private_stream.py $(ARGS)

test: _venv
	@.venv/bin/python -m pytest -q $(ARGS)

lint: _venv
	@.venv/bin/python -m ruff check src tests examples

format: _venv
	@.venv/bin/python -m ruff format src tests examples

format-check: _venv
	@.venv/bin/python -m ruff format --check src tests examples

check: lint format-check test
	@.venv/bin/python -m pip check
	@for script_path in scripts/*.sh; do sh -n "$$script_path" || exit $$?; done

env-template: _venv
	@.venv/bin/python -m bond_perpdex_client.setup --print-template

env-reference: _venv
	@.venv/bin/python -m bond_perpdex_client.setup --print-reference
