import asyncio

from app.services import os_detect


def test_generic_openssh_banner_uses_credentials_for_precise_linux_name(monkeypatch):
    async def fake_banner(*_args, **_kwargs):
        return "SSH-2.0-OpenSSH_9.9"

    async def fake_precise_probe(*_args, **_kwargs):
        return "Rocky Linux 10.0"

    monkeypatch.setattr(os_detect, "_grab_ssh_banner", fake_banner)
    monkeypatch.setattr(os_detect, "_probe_linux_os_via_ssh", fake_precise_probe)

    result = asyncio.run(
        os_detect.detect_os("192.0.2.10", username="root", password="secret")
    )

    assert result.os_system == "linux"
    assert result.os_version == "Rocky Linux 10.0"
    assert result.detail == "通过 SSH 登录识别: Rocky Linux 10.0"
