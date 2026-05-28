import argparse
import unittest
from unittest.mock import patch

from PyTado.http import DeviceActivationStatus

import sync_octopus_tado as sync


def make_args(**overrides):
    values = {
        "tado_email": "person@example.com",
        "tado_password": "secret",
        "octopus_api_key": "api-key",
        "mprn": None,
        "gas_serial_number": None,
        "octopus_account_number": "A-12345678",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self.payload = payload or {}
        self.text = text

    def json(self):
        return self.payload


class FakeTado:
    def __init__(self, status):
        self.status = status
        self.activated = False
        self.reading_kwargs = None

    def device_activation_status(self):
        return self.status

    def device_verification_url(self):
        return "https://login.tado.example/device"

    def device_activation(self):
        self.activated = True
        self.status = DeviceActivationStatus.COMPLETED

    def set_eiq_meter_readings(self, **kwargs):
        self.reading_kwargs = kwargs
        return {"ok": True}


class OctopusConsumptionTests(unittest.TestCase):
    def test_consumption_adds_pages_to_initial_meter_reading(self):
        responses = [
            FakeResponse(
                200,
                {
                    "results": [{"consumption": 1.2}, {"consumption": 2.3}],
                    "next": "https://api.octopus.energy/next-page",
                },
            ),
            FakeResponse(200, {"results": [{"consumption": 3.4}], "next": None}),
        ]

        with patch.object(sync.requests, "get", side_effect=responses) as get:
            total = sync.get_meter_reading_total_consumption(
                "api-key",
                "mprn",
                "serial",
                initial_meter_reading=100.0,
            )

        self.assertAlmostEqual(total, 106.9)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(
            get.call_args_list[0].args[0],
            "https://api.octopus.energy/v1/gas-meter-points/mprn/meters/serial/consumption/",
        )
        self.assertEqual(
            get.call_args_list[0].kwargs["params"],
            {"group_by": "quarter", "period_from": "2000-01-01T00:00:00Z"},
        )
        self.assertEqual(
            get.call_args_list[1].args[0], "https://api.octopus.energy/next-page"
        )
        self.assertIsNone(get.call_args_list[1].kwargs["params"])

    def test_consumption_url_escapes_meter_path_values(self):
        url = sync.build_octopus_consumption_url(" 1234567890 ", "E6S/12 34")

        self.assertEqual(
            url,
            "https://api.octopus.energy/v1/gas-meter-points/1234567890/meters/E6S%2F12%2034/consumption/",
        )

    def test_consumption_api_error_stops_without_returning_partial_reading(self):
        with patch.object(
            sync.requests,
            "get",
            return_value=FakeResponse(404, text="server exploded"),
        ):
            with self.assertRaisesRegex(RuntimeError, "OCTOPUS_MPRN"):
                sync.get_meter_reading_total_consumption(
                    "api-key",
                    "mprn",
                    "serial",
                    initial_meter_reading=100.0,
                )

    def test_account_discovery_uses_gas_meter_when_details_are_missing(self):
        responses = [
            FakeResponse(
                200,
                {
                    "properties": [
                        {
                            "moved_out_at": None,
                            "gas_meter_points": [
                                {
                                    "mprn": "1234567890",
                                    "meters": [{"serial_number": "GAS-123"}],
                                }
                            ],
                        }
                    ]
                },
            ),
            FakeResponse(200, {"results": [{"consumption": 4.5}], "next": None}),
        ]

        with patch.object(sync.requests, "get", side_effect=responses) as get:
            total = sync.get_meter_reading_total_consumption(
                "api-key",
                account_number="A-12345678",
                initial_meter_reading=100.0,
            )

        self.assertEqual(total, 104.5)
        self.assertEqual(
            get.call_args_list[0].args[0],
            "https://api.octopus.energy/v1/accounts/A-12345678/",
        )
        self.assertEqual(
            get.call_args_list[1].args[0],
            "https://api.octopus.energy/v1/gas-meter-points/1234567890/meters/GAS-123/consumption/",
        )

    def test_account_discovery_retries_after_configured_meter_404(self):
        responses = [
            FakeResponse(404, text="not found"),
            FakeResponse(
                200,
                {
                    "properties": [
                        {
                            "moved_out_at": None,
                            "gas_meter_points": [
                                {
                                    "mprn": "1234567890",
                                    "meters": [{"serial_number": "GAS-123"}],
                                }
                            ],
                        }
                    ]
                },
            ),
            FakeResponse(200, {"results": [{"consumption": 4.5}], "next": None}),
        ]

        with patch.object(sync.requests, "get", side_effect=responses):
            total = sync.get_meter_reading_total_consumption(
                "api-key",
                mprn="wrong-mprn",
                gas_serial_number="wrong-serial",
                account_number="A-12345678",
                initial_meter_reading=100.0,
            )

        self.assertEqual(total, 104.5)

    def test_missing_meter_details_and_account_number_fails_before_request(self):
        with patch.object(sync.requests, "get") as get:
            with self.assertRaisesRegex(RuntimeError, "OCTOPUS_ACCOUNT_NUMBER"):
                sync.get_meter_reading_total_consumption(
                    "api-key",
                    mprn=" ",
                    gas_serial_number="",
                )

        get.assert_not_called()


class ConfigValidationTests(unittest.TestCase):
    def test_validate_args_rejects_missing_secrets(self):
        args = make_args(
            tado_email="",
            tado_password=None,
            octopus_api_key=" ",
            octopus_account_number="",
            mprn="",
            gas_serial_number="",
        )

        with self.assertRaisesRegex(RuntimeError, "TADO_EMAIL"):
            sync.validate_args(args)

    def test_validate_args_allows_account_number_without_meter_details(self):
        sync.validate_args(make_args(octopus_account_number="A-12345678"))

    def test_validate_args_allows_explicit_meter_details_without_account_number(self):
        sync.validate_args(
            make_args(
                octopus_account_number=None,
                mprn="1234567890",
                gas_serial_number="GAS-123",
            )
        )


class TadoLoginTests(unittest.TestCase):
    def test_pending_activation_uses_browser_and_activates(self):
        fake_tado = FakeTado(DeviceActivationStatus.PENDING)
        browser_calls = []

        async def fake_browser_login(*args, **kwargs):
            browser_calls.append((args, kwargs))

        with patch.object(sync, "Tado", return_value=fake_tado), patch.object(
            sync, "browser_login", new=fake_browser_login
        ):
            result = sync.tado_login(
                "person@example.com",
                "secret",
                token_file_path="/tmp/test-token",
                browser_headless=True,
            )

        self.assertIs(result, fake_tado)
        self.assertTrue(fake_tado.activated)
        self.assertEqual(len(browser_calls), 1)
        self.assertEqual(
            browser_calls[0][0],
            ("https://login.tado.example/device", "person@example.com", "secret"),
        )
        self.assertEqual(browser_calls[0][1], {"headless": True})

    def test_completed_activation_does_not_launch_browser(self):
        fake_tado = FakeTado(DeviceActivationStatus.COMPLETED)

        with patch.object(sync, "Tado", return_value=fake_tado), patch.object(
            sync, "browser_login"
        ) as browser_login:
            result = sync.tado_login("person@example.com", "secret")

        self.assertIs(result, fake_tado)
        self.assertFalse(fake_tado.activated)
        browser_login.assert_not_called()

    def test_send_reading_uses_today_and_integer_reading(self):
        fake_tado = FakeTado(DeviceActivationStatus.COMPLETED)

        with patch.object(sync, "tado_login", return_value=fake_tado) as login:
            sync.send_reading_to_tado(
                "person@example.com",
                "secret",
                123.9,
                token_file_path="/tmp/test-token",
                manual_login=True,
                browser_headless=False,
            )

        login.assert_called_once_with(
            username="person@example.com",
            password="secret",
            token_file_path="/tmp/test-token",
            manual_login=True,
            browser_headless=False,
        )
        self.assertEqual(fake_tado.reading_kwargs["reading"], 123)
        self.assertEqual(
            fake_tado.reading_kwargs["date"], sync.date.today().isoformat()
        )


if __name__ == "__main__":
    unittest.main()
