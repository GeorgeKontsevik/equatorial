.PHONY: status compile test check

PYTHON := .venv/bin/python

status:
	git status --short --branch

compile:
	$(PYTHON) -m compileall -q src scripts

test:
	$(PYTHON) -m unittest discover -s tests -v

check: compile test
