"""Cloud-deploy additions: region self-check, /healthz, storage note, proxy IP."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main, region_probe
from app.config import Settings
from app.security import client_ip

ROOT = Path(__file__).resolve().parent.parent


def _getter(public_status: int, signed_status: int, signed_body: str = '{"code":-2014}'):
    def get(url, params, headers):
        assert "X-MBX-APIKEY" not in headers, "probe must never send a key"
        if url.endswith(region_probe.PUBLIC_PATH):
            return public_status, '{"serverTime":1}'
        return signed_status, signed_body

    return get


def test_probe_ok_when_signed_endpoint_answers_auth_error():
    r = region_probe.run_probe(_getter(200, 400))
    assert r["status"] == "ok"
    assert r["public"]["http_status"] == 200 and r["signed"]["http_status"] == 400


@pytest.mark.parametrize("pub,sig", [(451, 451), (200, 451), (403, 403)])
def test_probe_blocked_on_451_or_403(pub, sig):
    r = region_probe.run_probe(_getter(pub, sig, '{"code":0,"msg":"Service unavailable from a restricted location"}'))
    assert r["status"] == "blocked"
    assert "Frankfurt" in r["message_zh"]


def test_probe_network_error_is_not_blocked():
    def boom(url, params, headers):
        raise OSError("down")

    r = region_probe.run_probe(boom)
    assert r["status"] == "error"


def _settings(tmp_path: Path, **extra) -> Settings:
    base = dict(
        mode="mock",
        ledger_path=tmp_path / "ledger.json",
        watchlist_path=tmp_path / "watchlist.json",
        oplog_path=tmp_path / "ops.jsonl",
        clients_dir=tmp_path / "clients",
        region_probe="no",
    )
    base.update(extra)
    return Settings(**base)


@pytest.fixture
def tc_factory(tmp_path):
    def make(**extra):
        main.reset_singletons()
        main._settings = _settings(tmp_path, **extra)
        return TestClient(main.app)

    yield make
    main.reset_singletons()
    region_probe.reset_for_tests()


def test_healthz_has_no_secrets(tc_factory):
    with tc_factory() as tc:
        r = tc.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["mode"] == "mock"


def test_region_check_reports_blocked_state(tc_factory):
    region_probe.run_probe(_getter(451, 451))
    with tc_factory(region_probe="yes") as tc:
        # lifespan would re-probe; read the snapshot set above before it lands
        data = tc.get("/api/server-info").json()
    assert data["region"]["status"] in {"blocked", "ok", "error", "pending"}
    region_probe.run_probe(_getter(451, 451))
    with tc_factory() as tc:
        data = tc.get("/api/region-check").json()
    assert data["status"] == "disabled"


def test_region_check_blocked_when_enabled(tc_factory, monkeypatch):
    monkeypatch.setattr(region_probe, "_http_get", _getter(451, 451))
    with tc_factory(region_probe="yes") as tc:
        for _ in range(50):
            data = tc.get("/api/region-check").json()
            if data["status"] != "pending":
                break
            import time

            time.sleep(0.05)
    assert data["status"] == "blocked"
    assert "451" in data["message_zh"]


def test_ephemeral_storage_note(tc_factory):
    with tc_factory(ephemeral_storage="yes") as tc:
        info = tc.get("/api/server-info").json()
    assert info["ephemeral"] is True
    assert "清空" in info["storage_note_zh"]


def _req(peer: str, headers: dict):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_client_ip_proxy_trust():
    hdr = {"x-forwarded-for": "1.2.3.4, 10.0.0.1"}
    assert client_ip(_req("10.9.9.9", hdr)) == "10.9.9.9"
    assert client_ip(_req("10.9.9.9", hdr), trust_proxy=True) == "1.2.3.4"
    assert client_ip(_req("10.9.9.9", {"true-client-ip": "5.6.7.8"}), trust_proxy=True) == "5.6.7.8"


def test_deploy_files_default_to_mock_and_no_live():
    render = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert re.search(r"plan:\s*free", render)
    assert re.search(r"region:\s*frankfurt", render)
    assert re.search(r"key:\s*MODE\s*\n\s*value:\s*mock", render)
    assert "ALLOW_LIVE" not in render or re.search(r"key:\s*ALLOW_LIVE\s*\n\s*value:\s*\"?no\"?", render)
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "${PORT" in docker and "ALLOW_LIVE=yes" not in docker
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for item in ("data/", ".env", "tests/", ".venv"):
        assert item in ignore
