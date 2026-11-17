# make setup   create .venv and install the dependencies
# make test    run server.py and agent.py on this machine and try every command
PYTHON = .venv/bin/python

.PHONY: setup test

setup:
	python3 -m venv .venv
	$(PYTHON) -m pip install -r requirements.txt

test:
	$(PYTHON) test_smoke.py
