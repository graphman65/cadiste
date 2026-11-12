ARGS ?=

.PHONY: serve serve-reload

serve:
	uv run cadiste serve $(ARGS)

serve-reload:
	uv run watchfiles --target-type command 'cadiste serve $(ARGS)' src/cadiste
