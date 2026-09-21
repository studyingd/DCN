"""Device-owned remote credentials.

Global credential records were retired.  Secrets are encrypted directly on the
target device and are never exposed through a reusable credential catalogue.
"""

from app.services.crypto import decrypt, encrypt


def set_credentials(
    device,
    username: str | None,
    password: str | None = None,
    ssh_key: str | None = None,
) -> None:
    device.remote_username = username.strip() if username else None
    if password is not None:
        device.remote_password_enc = encrypt(password) if password else None
    if ssh_key is not None:
        device.remote_ssh_key_enc = encrypt(ssh_key) if ssh_key else None


def resolve_credentials(
    device,
    username: str | None = None,
    password: str | None = None,
    ssh_key: str | None = None,
) -> tuple[str, str, str]:
    """Return username, password and private key with request precedence."""
    return (
        username or getattr(device, "remote_username", None) or "",
        password
        if password is not None
        else (
            decrypt(device.remote_password_enc)
            if getattr(device, "remote_password_enc", None)
            else ""
        ),
        ssh_key
        if ssh_key is not None
        else (
            decrypt(device.remote_ssh_key_enc)
            if getattr(device, "remote_ssh_key_enc", None)
            else ""
        ),
    )
