"""Explicit cross-host profile acceptance: TLS 1.3/mTLS + strict JWT + CEL.

Run in an isolated environment with:
  LAS_RUN_GW_REMOTE=1 pytest tests/integration/test_agentgateway_remote.py
"""

from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn

jwt = pytest.importorskip("jwt")
x509 = pytest.importorskip("cryptography.x509")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: E402

from orchestrator.a2a_client import A2aClient  # noqa: E402

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_GW_REMOTE") != "1",
        reason="set LAS_RUN_GW_REMOTE=1 to run remote gateway acceptance",
    ),
]

ROOT = Path(__file__).resolve().parents[2]
AGW_BIN = ROOT / "infra" / "agentgateway" / "bin" / "agentgateway"
AGW_CONF = ROOT / "infra" / "agentgateway" / "config.remote.yaml"
ISSUER = "https://identity.agenthub.test/"
AUDIENCE = "agenthub-gateway"


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_private_key(path: Path, key) -> None:
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    path.chmod(0o600)


def _issue_certificate(
    *,
    common_name: str,
    public_key,
    issuer_name,
    issuer_key,
    is_ca: bool = False,
    server: bool = False,
    client: bool = False,
):
    now = dt.datetime.now(dt.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(issuer_name)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
    )
    usages = []
    if server:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
    if client:
        usages.append(ExtendedKeyUsageOID.CLIENT_AUTH)
    if usages:
        builder = builder.add_extension(x509.ExtendedKeyUsage(usages), critical=False)
    return builder.sign(private_key=issuer_key, algorithm=hashes.SHA256())


def _write_cert(path: Path, cert) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _b64uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _token(signing_key, *, agents: str, audience: str = AUDIENCE) -> str:
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": audience,
            "sub": "hermes-acceptance",
            "exp": int(time.time()) + 300,
            "nbf": int(time.time()) - 5,
            "role": "orchestrator",
            "agents": agents,
        },
        signing_key,
        algorithm="RS256",
        headers={"kid": "acceptance-key"},
    )


@dataclass(frozen=True)
class RemoteStack:
    gateway_base: str
    ca: Path
    client_cert: Path
    client_key: Path
    jwt_file: Path
    signing_key: object

    def client(self, token: str | None = None, *, with_cert: bool = True):
        cert = (str(self.client_cert), str(self.client_key)) if with_cert else None
        headers = {"Authorization": f"Bearer {token}"} if token else None
        return httpx.Client(
            verify=str(self.ca), cert=cert, headers=headers, timeout=3
        )


@pytest.fixture(scope="module")
def remote_stack(tmp_path_factory):
    if not AGW_BIN.exists():
        pytest.skip("agentgateway binary not installed")
    work = tmp_path_factory.mktemp("agentgateway-remote")
    workspace = work / "workspace"
    (workspace / "logs").mkdir(parents=True)
    os.environ["AGENT_WORKSPACE"] = str(workspace)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AgentHub test CA")])
    ca_cert = _issue_certificate(
        common_name="AgentHub test CA",
        public_key=ca_key.public_key(),
        issuer_name=ca_name,
        issuer_key=ca_key,
        is_ca=True,
    )
    ca_path = work / "ca.pem"
    _write_cert(ca_path, ca_cert)

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_cert = _issue_certificate(
        common_name="127.0.0.1",
        public_key=server_key.public_key(),
        issuer_name=ca_cert.subject,
        issuer_key=ca_key,
        server=True,
    )
    server_key_path = work / "server-key.pem"
    server_cert_path = work / "server.pem"
    _write_private_key(server_key_path, server_key)
    _write_cert(server_cert_path, server_cert)

    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client_cert = _issue_certificate(
        common_name="hermes-acceptance",
        public_key=client_key.public_key(),
        issuer_name=ca_cert.subject,
        issuer_key=ca_key,
        client=True,
    )
    client_key_path = work / "client-key.pem"
    client_cert_path = work / "client.pem"
    _write_private_key(client_key_path, client_key)
    _write_cert(client_cert_path, client_cert)

    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = signing_key.public_key().public_numbers()
    jwks_path = work / "jwks.json"
    jwks_path.write_text(json.dumps({"keys": [{
        "kty": "RSA", "use": "sig", "alg": "RS256", "kid": "acceptance-key",
        "n": _b64uint(numbers.n), "e": _b64uint(numbers.e),
    }]}), encoding="utf-8")
    jwt_file = work / "gateway.jwt"
    jwt_file.write_text(_token(signing_key, agents="codex,kimi,dsh"), encoding="utf-8")
    jwt_file.chmod(0o600)

    from adapters.fake.server import create_app

    worker_port = _free_loopback_port()
    gateway_port = _free_loopback_port()
    uvicorn_server = uvicorn.Server(uvicorn.Config(
        create_app(), host="127.0.0.1", port=worker_port, log_level="error"
    ))
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()

    config_path = work / "config.remote.yaml"
    config_path.write_text(
        AGW_CONF.read_text(encoding="utf-8").replace(
            "port: 8443", f"port: {gateway_port}", 1
        ),
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        GATEWAY_TLS_CERT_FILE=str(server_cert_path),
        GATEWAY_TLS_KEY_FILE=str(server_key_path),
        GATEWAY_CLIENT_CA_FILE=str(ca_path),
        GATEWAY_JWKS_FILE=str(jwks_path),
        GATEWAY_JWT_ISSUER=ISSUER,
        GATEWAY_JWT_AUDIENCE=AUDIENCE,
        AGENT_CODEX_BACKEND=f"127.0.0.1:{worker_port}",
        AGENT_KIMI_BACKEND="127.0.0.1:9",
        AGENT_DSH_BACKEND="127.0.0.1:9",
    )
    gateway = subprocess.Popen(
        [str(AGW_BIN), "-f", str(config_path)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    stack = RemoteStack(
        gateway_base=f"https://127.0.0.1:{gateway_port}",
        ca=ca_path,
        client_cert=client_cert_path,
        client_key=client_key_path,
        jwt_file=jwt_file,
        signing_key=signing_key,
    )
    try:
        for _ in range(50):
            try:
                with stack.client() as client:
                    if client.get(f"{stack.gateway_base}/agents/codex/health").status_code == 401:
                        break
            except httpx.TransportError:
                time.sleep(0.1)
        else:
            raise RuntimeError("remote agentgateway did not start")
        yield stack
    finally:
        gateway.terminate()
        gateway.wait(timeout=5)
        uvicorn_server.should_exit = True
        thread.join(timeout=5)


def test_mtls_rejects_missing_client_certificate(remote_stack):
    with remote_stack.client(with_cert=False) as client:
        with pytest.raises(httpx.TransportError):
            client.get(f"{remote_stack.gateway_base}/agents/codex/health")


def test_strict_jwt_rejects_missing_or_wrong_audience(remote_stack):
    with remote_stack.client() as client:
        assert client.get(
            f"{remote_stack.gateway_base}/agents/codex/health"
        ).status_code == 401
    wrong = _token(remote_stack.signing_key, agents="codex", audience="wrong")
    with remote_stack.client(wrong) as client:
        assert client.get(
            f"{remote_stack.gateway_base}/agents/codex/health"
        ).status_code == 401


def test_claim_acl_denies_unlisted_agent(remote_stack):
    token = _token(remote_stack.signing_key, agents="kimi")
    with remote_stack.client(token) as client:
        assert client.get(
            f"{remote_stack.gateway_base}/agents/codex/health"
        ).status_code == 403


@pytest.mark.anyio
async def test_hermes_client_completes_a2a_over_mtls_and_jwt(
    remote_stack, monkeypatch
):
    monkeypatch.setenv("LAS_GATEWAY_URL", remote_stack.gateway_base)
    monkeypatch.setenv("LAS_GATEWAY_JWT_FILE", str(remote_stack.jwt_file))
    monkeypatch.setenv("LAS_GATEWAY_CA_FILE", str(remote_stack.ca))
    monkeypatch.setenv("LAS_GATEWAY_CLIENT_CERT_FILE", str(remote_stack.client_cert))
    monkeypatch.setenv("LAS_GATEWAY_CLIENT_KEY_FILE", str(remote_stack.client_key))
    client = A2aClient.for_agent("codex", "http://unused", timeout=30)
    task = await client.send_and_wait(
        "remote gateway acceptance task",
        idempotency_key="T-REMOTE-GW:1",
    )
    assert task["status"]["state"] == "completed"
