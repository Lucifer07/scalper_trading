PY := .venv/bin/python

.PHONY: venv test run sim mcp gui

venv:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt

test:
	$(PY) -m pytest tests -q

run:
	$(PY) -m xau_monitor.main

# Simulator EA MT4 (tanpa terminal) — untuk menguji jalur order.
sim:
	$(PY) -m tools.fake_ea

# MCP MT4 read-only (tambah --allow-trading untuk tool order).
mcp:
	$(PY) -m tools.mcp_mt4

# Panel GUI lokal (http://127.0.0.1:8773) — menyalakan/mematikan monitor.
gui:
	$(PY) -m xau_monitor.panel
