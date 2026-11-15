.PHONY: install dev test lint contracts demo migrate revision up down sim clean
PY ?= python

install:            ## install runtime + dev deps
	$(PY) -m pip install -e ".[dev,postgres]"

dev:                ## API + simulated chamber, zero external services
	MYCOLOOP_SIM_AUTOSTART=1 MYCOLOOP_BUS=loopback $(PY) -m uvicorn mycoloop.main:app --host 0.0.0.0 --port 8000 --reload

test:               ## 62 tests: contracts, command bus, quality, biology, safety, optimizer gate
	$(PY) -m pytest -q

contracts:          ## export openapi + asyncapi + json schemas
	$(PY) scripts/export_contracts.py

demo:               ## seed history + live cycle into a running API
	$(PY) scripts/demo.py --api http://localhost:8000

migrate:            ## apply migrations
	$(PY) -m alembic upgrade head

revision:           ## autogenerate a migration: make revision m="add x"
	$(PY) -m alembic revision --autogenerate -m "$(m)"

up:                 ## postgres + timescale + mosquitto + api
	docker compose up -d --build

sim:                ## add a simulated chamber on the real broker
	docker compose --profile sim up -d

down:
	docker compose down

clean:
	rm -f *.db *.db-wal *.db-shm
