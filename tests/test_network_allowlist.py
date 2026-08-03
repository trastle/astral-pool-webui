import ipaddress
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import _client_allowed
from config import parse_allowed_networks

DEFAULT_NETWORKS = parse_allowed_networks(
    ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128"]
)


def test_parse_allowed_networks_parses_valid_cidrs():
    networks = parse_allowed_networks(["10.0.0.0/8", "::1/128"])
    assert networks == [ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("::1/128")]


def test_parse_allowed_networks_rejects_invalid_cidr():
    with pytest.raises(SystemExit):
        parse_allowed_networks(["not-a-cidr"])


@pytest.mark.parametrize(
    "host",
    ["10.1.2.3", "172.20.0.1", "192.168.1.50", "127.0.0.1", "::1"],
)
def test_client_allowed_for_private_and_loopback_addresses(host):
    assert _client_allowed(host, DEFAULT_NETWORKS) is True


@pytest.mark.parametrize(
    "host",
    ["8.8.8.8", "1.1.1.1", "203.0.113.5", "2001:db8::1"],
)
def test_client_allowed_rejects_public_addresses(host):
    assert _client_allowed(host, DEFAULT_NETWORKS) is False


def test_client_allowed_unwraps_ipv4_mapped_ipv6():
    # Dual-stack listeners can report an IPv4 peer as ::ffff:a.b.c.d.
    assert _client_allowed("::ffff:192.168.1.1", DEFAULT_NETWORKS) is True
    assert _client_allowed("::ffff:8.8.8.8", DEFAULT_NETWORKS) is False


def test_client_allowed_rejects_missing_or_unparseable_host():
    assert _client_allowed(None, DEFAULT_NETWORKS) is False
    assert _client_allowed("", DEFAULT_NETWORKS) is False
    assert _client_allowed("testclient", DEFAULT_NETWORKS) is False


def test_middleware_blocks_disallowed_client_with_403():
    # BleakScanner mocked so entering the app's lifespan (which kicks off
    # the poll loop) never attempts a real BLE scan - see test_routes.py.
    with patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=None)):
        with TestClient(app_module.app, client=("8.8.8.8", 12345)) as client:
            resp = client.get("/")
    assert resp.status_code == 403


def test_middleware_allows_configured_client():
    with patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=None)):
        with TestClient(app_module.app, client=("127.0.0.1", 12345)) as client:
            resp = client.get("/")
    assert resp.status_code == 200
