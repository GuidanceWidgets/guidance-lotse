# Guidance Widgets integration

This package exposes native Lotse strategies through the Guidance Widgets HTTP
contract. It accepts provenance snapshots, returns `GuidanceResult` objects, and
forwards accept, reject, and snooze feedback to the original Lotse actions.

## Configuration

Set the strategy and state-vector directories before creating the application:

```text
LOTSE_STRATEGY_PATH=/path/to/strategy_configs
LOTSE_STATE_PATH=/path/to/state_vector
```

Optional variables:

- `LOTSE_GUIDANCE_TOP_N`: maximum number of suggestions returned; defaults to `3`.
- `LOTSE_ALLOWED_ORIGINS`: comma-separated browser origins.
- `LOTSE_PLAYGROUND_ENABLED=1`: enables the local YAML playground endpoints.

## Run

```text
python -m uvicorn lotse.integrations.guidance_widgets.api:create_app \
  --factory --host 127.0.0.1 --port 8021
```


