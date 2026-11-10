import io
import json
import logging
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PyTado.http import DeviceActivationStatus

import tado_auth as auth

SEED = "example-placeholder-do-not-use"
ROTATED = "example-rotated-placeholder-do-not-use"


class TokenStoreTests(unittest.TestCase):
    def test_activation_polling_has_a_timeout_and_keeps_tls_validation(self):
        with patch.object(requests.Session, "request") as request:
            auth.ActivationSession().request(
                "POST", "https://login.tado.com/oauth2/token"
            )
        self.assertEqual(request.call_args.kwargs["timeout"], 30)
        self.assertNotIn("verify", request.call_args.kwargs)

    def test_accepts_raw_and_pytado_json_without_exposing_values(self):
        self.assertEqual(auth.normalize_token(SEED), SEED)
        self.assertEqual(
            auth.normalize_token(json.dumps({"refresh_token": SEED})), SEED
        )
        for invalid in (
            None,
            "",
            "{}",
            "{not-json}",
            '{"refresh_token":null}',
            '{"refresh_token":123}',
            "bad token",
            SEED + "\nINJECT",
            "x" * 16385,
        ):
            with self.subTest(invalid_type=type(invalid).__name__):
                with self.assertRaises(auth.TadoAuthError):
                    auth.normalize_token(invalid)

    def make_store(self):
        with patch.object(auth.shutil, "which", return_value="/usr/bin/gh"):
            return auth.GitHubTokenStore("seanor1701/octopus-to-tado-sync")

    def test_token_is_only_sent_on_stdin_and_unrelated_secrets_are_not_forwarded(self):
        with patch.dict(
            os.environ,
            {
                "GH_DEBUG": "api",
                "OCTOPUS_API_KEY": SEED,
                "TADO_TOKEN_STORE_TOKEN": "example-github-placeholder-do-not-use",
            },
        ):
            store = self.make_store()
        with patch.object(auth.subprocess, "run") as run:
            store(ROTATED)
        args, kwargs = run.call_args
        self.assertNotIn(ROTATED, repr(args))
        self.assertEqual(kwargs["input"], ROTATED)
        self.assertEqual(kwargs["env"]["GH_HOST"], "github.com")
        self.assertNotIn("GH_DEBUG", kwargs["env"])
        self.assertNotIn("OCTOPUS_API_KEY", kwargs["env"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 30)

    def test_ci_requires_explicit_scoped_writer_before_consuming_token(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            auth.shutil, "which", return_value="gh"
        ):
            with self.assertRaisesRegex(auth.TadoAuthError, "TADO_TOKEN_STORE_TOKEN"):
                auth.GitHubTokenStore(
                    "seanor1701/octopus-to-tado-sync", require_writer=True
                )

    def test_missing_cli_fails_closed(self):
        with patch.object(auth.shutil, "which", return_value=None):
            with self.assertRaises(auth.TadoAuthError):
                auth.GitHubTokenStore("seanor1701/octopus-to-tado-sync")

    def test_repository_cannot_redirect_secret_to_another_host(self):
        with self.assertRaises(auth.TadoAuthError):
            auth.GitHubTokenStore("https://example.invalid/repo")

    def test_write_failure_is_retried_boundedly_without_leaking_stderr(self):
        store = self.make_store()
        with patch.object(
            auth.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, "gh", stderr=SEED),
        ), patch.object(auth.time, "sleep"):
            with self.assertRaises(auth.TadoAuthError) as error:
                store(ROTATED)
        self.assertNotIn(SEED, str(error.exception))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = Mock()
        self.path = None
        self.events = []

    def factory(self, token_file_path, saved_refresh_token, http_session):
        self.path = Path(token_file_path)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.path.write_text(json.dumps({"refresh_token": ROTATED}))
        client = Mock()
        client.device_activation_status.return_value = DeviceActivationStatus.COMPLETED
        return client

    def test_persists_before_reading_and_cleans_up_private_file(self):
        self.store.side_effect = lambda token: self.events.append("saved")
        with patch.object(auth, "Tado", side_effect=self.factory):
            with auth.tado_session(SEED, persist=self.store) as client:
                self.events.append("reading")
                client.set_eiq_meter_readings(reading=123)
                self.assertTrue(self.path.exists())
        self.assertEqual(self.events, ["saved", "reading"])
        self.store.preflight.assert_called_once_with(SEED)
        self.assertFalse(self.path.parent.exists())

    def test_second_rotation_is_saved_even_when_meter_api_fails(self):
        with patch.object(auth, "Tado", side_effect=self.factory):
            with self.assertRaises(auth.TadoAuthError):
                with auth.tado_session(SEED, persist=self.store):
                    self.path.write_text(
                        json.dumps(
                            {"refresh_token": "example-second-placeholder-do-not-use"}
                        )
                    )
                    raise requests.ConnectionError(SEED)
        self.assertEqual(
            self.store.call_args_list[-1].args[0],
            "example-second-placeholder-do-not-use",
        )
        self.assertFalse(self.path.parent.exists())

    def test_rotation_survives_constructor_failure_after_refresh(self):
        def fail(**kwargs):
            self.factory(**kwargs)
            raise requests.ConnectionError(SEED)

        with patch.object(auth, "Tado", side_effect=fail):
            with self.assertRaises(auth.TadoAuthError) as error:
                with auth.tado_session(SEED, persist=self.store):
                    self.fail("must not yield")
        self.store.assert_called_once_with(ROTATED)
        self.assertNotIn(SEED, str(error.exception))

    def test_failed_preflight_does_not_consume_token(self):
        self.store.preflight.side_effect = auth.TadoAuthError("No write access")
        with patch.object(auth, "Tado") as tado:
            with self.assertRaises(auth.TadoAuthError):
                with auth.tado_session(SEED, persist=self.store):
                    self.fail("must not yield")
        tado.assert_not_called()

    def test_failed_persistence_prevents_reading(self):
        self.store.side_effect = auth.TadoAuthError("Write failed")
        with patch.object(auth, "Tado", side_effect=self.factory):
            with self.assertRaises(auth.TadoAuthError):
                with auth.tado_session(SEED, persist=self.store):
                    self.fail("must not yield")
        self.assertFalse(self.path.parent.exists())

    def test_rejected_pending_or_unknown_status_never_yields_success(self):
        for status in (
            DeviceActivationStatus.NOT_STARTED,
            DeviceActivationStatus.PENDING,
            "FAILED",
        ):
            client = Mock()
            client.device_activation_status.return_value = status
            with patch.object(auth, "Tado", return_value=client):
                with self.assertRaises(auth.TadoAuthError):
                    with auth.tado_session(SEED, persist=self.store):
                        self.fail("must not yield")
            client.set_eiq_meter_readings.assert_not_called()

    def test_missing_token_fails_before_any_network(self):
        with patch.object(auth, "Tado") as tado:
            with self.assertRaises(auth.TadoAuthError):
                with auth.tado_session(persist=self.store):
                    self.fail("must not yield")
        tado.assert_not_called()

    def test_ci_cannot_open_interactive_authorization(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), patch.object(
            auth, "Tado"
        ) as tado:
            with self.assertRaises(auth.TadoAuthError):
                with auth.tado_session(interactive=True, persist=self.store):
                    self.fail("must not yield")
        tado.assert_not_called()

    def test_interactive_approval_opens_browser_without_logging_url_or_token(self):
        def factory(**kwargs):
            client = self.factory(**kwargs)
            client.device_activation_status.side_effect = [
                DeviceActivationStatus.PENDING,
                DeviceActivationStatus.COMPLETED,
            ]
            client.device_verification_url.return_value = "https://login.tado.com/oauth2/device?user_code=example-placeholder-do-not-use"
            return client

        output = io.StringIO()
        with patch.object(auth, "Tado", side_effect=factory), patch.object(
            auth.webbrowser, "open", return_value=True
        ) as browser, redirect_stdout(output), redirect_stderr(output):
            with auth.tado_session(interactive=True, persist=self.store):
                pass
        browser.assert_called_once()
        self.assertNotIn("https://", output.getvalue())
        self.assertNotIn(ROTATED, output.getvalue())

    def test_expired_activation_is_sanitized_and_does_not_submit(self):
        client = Mock()
        client.device_activation_status.return_value = DeviceActivationStatus.PENDING
        client.device_verification_url.return_value = (
            "https://login.tado.com/oauth2/device?user_code=example"
        )
        client.device_activation.side_effect = RuntimeError("expired " + SEED)
        with patch.object(auth, "Tado", return_value=client), patch.object(
            auth.webbrowser, "open", return_value=True
        ):
            with self.assertRaises(auth.TadoAuthError) as error:
                with auth.tado_session(interactive=True, persist=self.store):
                    self.fail("must not yield")
        self.assertNotIn(SEED, str(error.exception))
        client.set_eiq_meter_readings.assert_not_called()

    def test_unexpected_browser_host_is_rejected(self):
        client = Mock()
        client.device_activation_status.return_value = DeviceActivationStatus.PENDING
        client.device_verification_url.return_value = (
            "https://example.invalid/oauth2/device?user_code=example"
        )
        with patch.object(auth, "Tado", return_value=client), patch.object(
            auth.webbrowser, "open"
        ) as browser:
            with self.assertRaisesRegex(auth.TadoAuthError, "unexpected"):
                with auth.tado_session(interactive=True, persist=self.store):
                    self.fail("must not yield")
        browser.assert_not_called()

    def test_persistence_failure_still_restores_logging_and_removes_file(self):
        previous = auth.pytado_http._LOGGER.disabled
        self.store.side_effect = auth.TadoAuthError("write failed")
        with patch.object(auth, "Tado", side_effect=self.factory):
            with self.assertRaises(auth.TadoAuthError):
                with auth.tado_session(SEED, persist=self.store):
                    self.fail("must not yield")
        self.assertEqual(auth.pytado_http._LOGGER.disabled, previous)
        self.assertFalse(self.path.parent.exists())


class PyTadoContractTests(unittest.TestCase):
    """Exercise the installed library with mocked OAuth and home APIs."""

    def test_three_consecutive_runs_use_latest_saved_token(self):
        latest = [SEED]
        store = Mock()
        store.side_effect = lambda token: latest.__setitem__(0, token)

        def response(payload):
            result = requests.Response()
            result.status_code = 200
            result._content = json.dumps(payload).encode()
            return result

        def refresh(method, url, **kwargs):
            self.assertEqual(url, "https://login.tado.com/oauth2/token")
            self.assertEqual(kwargs["params"]["refresh_token"], latest[0])
            return response(
                {
                    "access_token": "example-placeholder-do-not-use",
                    "expires_in": 600,
                    "refresh_token": latest[0] + "-rotated",
                }
            )

        def home_send(session, request, **kwargs):
            self.assertEqual(request.method, "GET")
            self.assertTrue(request.url.startswith("https://my.tado.com/api/v2/"))
            return response(
                {"homes": [{"id": 1}]} if request.url.endswith("/me") else {}
            )

        with patch.dict(os.environ, {}, clear=True), patch.object(
            requests.Session, "request", side_effect=refresh
        ), patch.object(requests.Session, "send", autospec=True, side_effect=home_send):
            for _ in range(3):
                with auth.tado_session(latest[0], persist=store) as tado:
                    self.assertEqual(
                        tado.device_activation_status(),
                        DeviceActivationStatus.COMPLETED,
                    )
        self.assertEqual(latest[0], SEED + "-rotated" * 3)
        self.assertEqual(store.call_count, 3)

    def test_real_constructor_http_400_does_not_report_success(self):
        result = requests.Response()
        result.status_code = 400
        result._content = b'{"error":"invalid_grant"}'
        output = io.StringIO()
        with patch.object(
            requests.Session, "request", return_value=result
        ), redirect_stdout(output), redirect_stderr(output):
            with self.assertRaisesRegex(auth.TadoAuthError, "rejected"):
                with auth.tado_session(SEED, persist=Mock()):
                    self.fail("must not yield")
        self.assertNotIn(SEED, output.getvalue())
        self.assertNotIn("confirmed", output.getvalue())
