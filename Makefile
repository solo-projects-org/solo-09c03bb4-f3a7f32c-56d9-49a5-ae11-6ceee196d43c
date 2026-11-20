.PHONY: up down test local

up:
	docker compose up --build

down:
	docker compose down

test:
	python3 -m unittest discover -s tests -v

local:
	DATABASE_PATH=./data/prices.db python3 run.py
