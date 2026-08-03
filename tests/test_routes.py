"""End-to-end smoke test of the actual FastAPI app object: routes, lifespan
startup/shutdown (which starts the poll loop and connects the MQTT bridge),
not just the underlying functions in isolation. BleakScanner is mocked so
this never does a real 15s BLE scan on whatever machine runs the tests.

Assertions are deliberately loose about *content* (not "waiting" vs a real
reading) since app.latest_state is a module-level global shared across the
whole test session - other tests may have already populated it by the time
this runs. What matters here is that the routes/lifespan wire up and
respond correctly, which the more specific unit tests elsewhere already
cover for content.
"""
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app as app_module


def test_app_lifecycle_and_routes_respond():
    # TestClient's fake client address defaults to ("testclient", 50000) -
    # not a real IP, so it'd get 403'd by restrict_to_allowed_networks()
    # unless given a real, allowed one explicitly.
    with patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=None)):
        with TestClient(app_module.app, client=("127.0.0.1", 12345)) as client:
            index_resp = client.get("/")
            assert index_resp.status_code == 200
            assert "Pool Chlorinator" in index_resp.text

            help_resp = client.get("/help")
            assert help_resp.status_code == 200
            assert "astralpool.com.au" in help_resp.text

            metrics_resp = client.get("/metrics")
            assert metrics_resp.status_code == 200
            assert "chlorinator_scrape_success" in metrics_resp.text
