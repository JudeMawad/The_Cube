# Development and tests

Tests are grouped by service and use separate environments. HTTP contract tests may import both services to check serialization; runtime services do not import each other.

## Hardware-free test setup

Use Python 3.13, venv support and a C++17 compiler (`g++`) for native client tests. On Linux, libsndfile is needed if your SoundFile wheel does not bundle it. No Pi, GPU, credentials or downloaded model weights are required. Run from the repository root:

```sh
python3 -m venv /tmp/cube-client-tests
/tmp/cube-client-tests/bin/python -m pip install -r tests/requirements-client.txt
PYTHONPATH=client/app /tmp/cube-client-tests/bin/python -m unittest discover -s tests/client -t . -v

python3 -m venv /tmp/cube-server-tests
/tmp/cube-server-tests/bin/python -m pip install -r tests/requirements-server.txt
PYTHONPATH=server CUBE_AI_NODE_URL= /tmp/cube-server-tests/bin/python -m unittest discover -s tests/server -t . -v

python3 -m venv /tmp/cube-ai-tests
/tmp/cube-ai-tests/bin/python -m pip install -r tests/requirements-ai-node.txt
/tmp/cube-ai-tests/bin/python -m unittest discover -s tests/ai_node -t . -v

python3 -m venv /tmp/cube-contract-tests
/tmp/cube-contract-tests/bin/python -m pip install -r tests/requirements-integration.txt
PYTHONPATH=server:client/app /tmp/cube-contract-tests/bin/python -m unittest discover -s tests/integration -t . -v
```

The test requirements files install CPU-only development dependencies. Use each service's own requirements file for deployment. AI tests mock inference loading while retaining real API/lifecycle behavior. Local loopback sockets and worker threads are required by cancellation tests; restrictive execution sandboxes can block them. Tests never contact installed Cube services.

With already-provisioned production environments, the equivalent commands are:

```sh
PYTHONPATH=client/app client/app/.venv/bin/python -m unittest discover -s tests/client -t . -v
PYTHONPATH=server CUBE_AI_NODE_URL= server/.venv/bin/python -m unittest discover -s tests/server -t . -v
ai_node/.venv/bin/python -m unittest discover -s tests/ai_node -t . -v
```

Integration tests use the separate development environment above. Pytest is optional; `pytest.ini` selects this same tree and import roots. Do not combine CPU/GPU production requirements to run tests. New tests should mock external services at their transport/inference boundary, retain safety assertions and avoid testing deleted implementations.

## Repository and native checks

```sh
python3 scripts/build_voice_catalog.py --check
python3 scripts/check_docs.py
make -C client/native/cube-display -j2
```

The documentation check resolves links into the pinned dependency, so initialize it with `git submodule update --init --recursive` first. The native build requires the pinned submodule, `make` and `patch`; it only creates a candidate. Native unit tests compile an inert matrix facade and synthetic parser/render fixtures, never opening the panel. Original bundled sample assets are tested independently of third-party artwork.

`CUBE_TEST_ISOLATED_PIPEWIRE=1` opts into isolated PipeWire tests; they are skipped by default. Physical panel/audio, ReSpeaker AEC, Bluetooth, GPU inference/cancellation, live Spotify, smart-home and media workflows require [manual acceptance](../docs/PROJECT_STATUS.md). Passing unit tests does not verify those systems.

CI selects all four suites with their own dependency manifest. Changes to HTTP schemas, cancellation or control receipts require cross-service tests as well as the affected component tests.
