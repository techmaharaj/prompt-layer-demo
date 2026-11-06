# Thin aliases. Everything real lives in ./setup.sh and ./capability.
.PHONY: help setup reset verify list clean

help:
	@echo "make setup    ./setup.sh             venv, deps, the \`capability\` command, self-test"
	@echo "make list     ./capability list      the catalogue"
	@echo "make reset    ./capability reset     back to the starting point"
	@echo "make verify   ./capability verify    self-test, non-zero exit on failure"
	@echo "make clean                           remove the venv and local run artefacts"
	@echo ""
	@echo "The two web tools are the demo; see README \"Run it yourself\"."

setup:
	@./setup.sh

reset:
	@./capability reset

verify:
	@./capability verify

list:
	@./capability list

clean:
	rm -rf .venv *.egg-info fixtures/ledger.json fixtures/boundary-log.jsonl
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
