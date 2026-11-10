"""Device authorization and durable storage of rotating Tado refresh tokens."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import requests
import PyTado.http as pytado_http
from PyTado.http import DeviceActivationStatus
from PyTado.interface import Tado


class TadoAuthError(RuntimeError):
    pass


class ActivationSession(requests.Session):
    """Bound device-code polling; retain Requests' default TLS verification."""

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", 30)
        return super().request(*args, **kwargs)


def normalize_token(value):
    """Accept the raw secret or PyTado's JSON export without logging either."""
    if not isinstance(value, str) or not value.strip() or len(value) > 16384:
        raise TadoAuthError("TADO_REFRESH_TOKEN is missing or malformed.")
    value = value.strip()
    if value.startswith("{"):
        try:
            value = json.loads(value)["refresh_token"]
        except (ValueError, KeyError, TypeError):
            raise TadoAuthError("TADO_REFRESH_TOKEN JSON is malformed.") from None
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9._~+/-]+={0,2}", value
    ):
        raise TadoAuthError("TADO_REFRESH_TOKEN is malformed.")
    return value


class GitHubTokenStore:
    """Use gh's sealed-box encryption; secrets travel only via standard input."""

    def __init__(self, repository, environment="Secrets", require_writer=False):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise TadoAuthError("Invalid GitHub repository for token storage.")
        self.repository = repository
        self.environment = environment
        self.executable = shutil.which("gh")
        if not self.executable:
            raise TadoAuthError("GitHub CLI is required to save the refreshed token.")
        # Do not forward debug settings or unrelated account/API credentials.
        self.env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
        self.env.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1")
        writer = os.environ.get("TADO_TOKEN_STORE_TOKEN", "").strip()
        if require_writer and not writer:
            raise TadoAuthError(
                "Missing TADO_TOKEN_STORE_TOKEN. Configure a fine-grained GitHub "
                "token for this repository with Environments: read and write."
            )
        if writer:
            self.env["GH_TOKEN"] = writer

    def _run(self, arguments, token=None):
        try:
            subprocess.run(
                [self.executable, *arguments],
                input=token,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self.env,
                check=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            raise TadoAuthError(
                "Could not save the Tado token in GitHub. Check the selected "
                "account and token's Environments write permission."
            ) from None

    def preflight(self, seed=None):
        if seed:
            # Prove write access BEFORE consuming a potentially single-use token.
            self(seed)
        else:
            self._run(
                [
                    "api",
                    f"repos/{self.repository}/environments/{self.environment}/secrets/public-key",
                ]
            )

    def __call__(self, token):
        token = normalize_token(token)
        for attempt in range(3):
            try:
                self._run(
                    [
                        "secret",
                        "set",
                        "TADO_REFRESH_TOKEN",
                        "--env",
                        self.environment,
                        "--repo",
                        self.repository,
                    ],
                    token,
                )
                return
            except TadoAuthError:
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)


@contextmanager
def tado_session(refresh_token=None, interactive=False, persist=None):
    """Save replacement tokens before use and on failure; never expose them."""
    seed = normalize_token(refresh_token) if refresh_token else None
    if os.environ.get("GITHUB_ACTIONS") == "true" and interactive:
        raise TadoAuthError("Interactive Tado authorization is unavailable in CI.")
    if not seed and not interactive:
        raise TadoAuthError("TADO_REFRESH_TOKEN is missing. Run local authorization.")
    if persist is None:
        raise TadoAuthError("A token store is required for authorization.")
    persist.preflight(seed)

    # PyTado writes JSON on every successful refresh. Keep it in a private,
    # job-local directory, never a cache, artifact, screenshot, or checkout file.
    with tempfile.TemporaryDirectory(prefix="tado-auth-") as directory:
        token_path = Path(directory) / "token.json"
        last_saved = seed
        # The pinned library constructs its own Logger instead of registering
        # it with logging.getLogger(), so silence that actual logger instance.
        logger = pytado_http._LOGGER
        previous_disabled = logger.disabled
        logger.disabled = True  # Library diagnostics can contain OAuth URLs/headers.

        def save_replacement():
            nonlocal last_saved
            if not token_path.exists():
                return
            try:
                data = json.loads(token_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise TadoAuthError(
                    "Could not read the replacement Tado token."
                ) from None
            if not isinstance(data, dict):
                raise TadoAuthError("Tado returned an invalid token file.")
            if "refresh_token" in data:
                token = normalize_token(data["refresh_token"])
                if token != last_saved:
                    if persist is None:
                        raise TadoAuthError(
                            "A token store is required for authorization."
                        )
                    persist(token)
                    last_saved = token

        try:
            tado = Tado(
                token_file_path=str(token_path),
                saved_refresh_token=seed,
                http_session=ActivationSession(),
            )
            if tado.device_activation_status() == DeviceActivationStatus.PENDING:
                if not interactive:
                    raise TadoAuthError(
                        "Tado authorization is pending; authorize locally."
                    )
                url = tado.device_verification_url()
                parsed = urlsplit(url or "")
                if (
                    parsed.scheme != "https"
                    or parsed.netloc != "login.tado.com"
                    or parsed.path != "/oauth2/device"
                ):
                    raise TadoAuthError(
                        "Tado returned an unexpected authorization address."
                    )
                if not webbrowser.open(url):
                    raise TadoAuthError("Could not open the browser for Tado approval.")
                print(
                    "Approve Tado in the browser. Waiting for authorization...",
                    flush=True,
                )
                tado.device_activation()
            if tado.device_activation_status() != DeviceActivationStatus.COMPLETED:
                raise TadoAuthError(
                    "Tado rejected the refresh token. Complete local authorization again."
                )
            save_replacement()
            if not token_path.exists() or "refresh_token" not in json.loads(
                token_path.read_text(encoding="utf-8")
            ):
                raise TadoAuthError("Tado did not return a refresh token.")
            yield tado
        except TadoAuthError:
            raise
        except Exception:
            raise TadoAuthError(
                "Tado authorization or request failed. Authorization may have "
                "expired or been rejected. No automatic reading retry was attempted."
            ) from None
        finally:
            try:
                # Also retain a token rotated just before a later API failure.
                if token_path.exists():
                    save_replacement()
            finally:
                logger.disabled = previous_disabled
