import argparse
import asyncio
import os
from datetime import date, datetime
from urllib.parse import quote

import requests
from requests.auth import HTTPBasicAuth
from PyTado.http import DeviceActivationStatus
from PyTado.interface import Tado

DEFAULT_TADO_TOKEN_FILE = "/tmp/tado_refresh_token"
DEFAULT_INITIAL_METER_READING = 6537.9
OCTOPUS_API_BASE_URL = "https://api.octopus.energy/v1"


class OctopusApiError(RuntimeError):
    def __init__(self, status_code, message):
        self.status_code = status_code
        super().__init__(message)


def optional_text(value):
    if value is None:
        return None

    value = str(value).strip()
    return value or None


def response_message(response):
    text = response.text.strip()
    if len(text) > 500:
        return f"{text[:500]}..."

    return text


def build_octopus_consumption_url(mprn, gas_serial_number):
    mprn_path = quote(str(mprn).strip(), safe="")
    serial_path = quote(str(gas_serial_number).strip(), safe="")

    return (
        f"{OCTOPUS_API_BASE_URL}/gas-meter-points/{mprn_path}/"
        f"meters/{serial_path}/consumption/"
    )


def build_octopus_account_url(account_number):
    account_path = quote(str(account_number).strip(), safe="")
    return f"{OCTOPUS_API_BASE_URL}/accounts/{account_path}/"


def get_octopus_account(api_key, account_number):
    response = requests.get(
        build_octopus_account_url(account_number),
        auth=HTTPBasicAuth(api_key, ""),
        timeout=30,
    )

    if response.status_code != 200:
        raise OctopusApiError(
            response.status_code,
            f"Failed to retrieve Octopus account data. Status code: {response.status_code}, "
            f"Message: {response_message(response)}",
        )

    return response.json()


def get_gas_meter_candidates(account):
    candidates = []

    for property_data in account.get("properties", []):
        is_active_property = property_data.get("moved_out_at") is None

        for meter_point in property_data.get("gas_meter_points", []):
            mprn = optional_text(meter_point.get("mprn"))
            if not mprn:
                continue

            for meter in meter_point.get("meters", []):
                serial_number = optional_text(meter.get("serial_number"))
                if not serial_number:
                    continue

                candidates.append(
                    {
                        "mprn": mprn,
                        "gas_serial_number": serial_number,
                        "is_active_property": is_active_property,
                    }
                )

    candidates.sort(key=lambda candidate: not candidate["is_active_property"])
    return candidates


def describe_gas_meter_candidates(candidates):
    if not candidates:
        return "No gas meters were found on the Octopus account."

    active_count = sum(1 for candidate in candidates if candidate["is_active_property"])
    return (
        f"Found {len(candidates)} gas meter candidate(s), "
        f"{active_count} on active properties."
    )


def get_meter_consumption_from_octopus(
    api_key,
    mprn,
    gas_serial_number,
    initial_meter_reading,
):
    period_from = datetime(2000, 1, 1, 0, 0, 0)
    url = build_octopus_consumption_url(mprn, gas_serial_number)
    params = {
        "group_by": "quarter",
        "period_from": f"{period_from.isoformat()}Z",
    }
    total_consumption = initial_meter_reading

    while url:
        response = requests.get(
            url,
            auth=HTTPBasicAuth(api_key, ""),
            params=params,
            timeout=30,
        )
        params = None

        if response.status_code != 200:
            hint = ""
            if response.status_code == 404:
                hint = (
                    " Check OCTOPUS_MPRN and OCTOPUS_GAS_SERIAL match the gas meter "
                    "details shown in your Octopus API dashboard, or set "
                    "OCTOPUS_ACCOUNT_NUMBER so they can be discovered automatically."
                )
            raise OctopusApiError(
                response.status_code,
                f"Failed to retrieve Octopus data. Status code: {response.status_code}, "
                f"Message: {response_message(response)}{hint}",
            )

        meter_readings = response.json()
        total_consumption += sum(
            interval["consumption"] for interval in meter_readings["results"]
        )
        url = meter_readings.get("next") or ""

    return total_consumption


def get_meter_reading_total_consumption(
    api_key,
    mprn=None,
    gas_serial_number=None,
    account_number=None,
    initial_meter_reading=DEFAULT_INITIAL_METER_READING,
):
    """
    Retrieves total gas consumption from the Octopus Energy API for the given gas meter point and serial number.
    """
    mprn = optional_text(mprn)
    gas_serial_number = optional_text(gas_serial_number)
    account_number = optional_text(account_number)

    if mprn and gas_serial_number:
        try:
            total_consumption = get_meter_consumption_from_octopus(
                api_key,
                mprn,
                gas_serial_number,
                initial_meter_reading,
            )
            print(f"Total consumption is {total_consumption}")
            return total_consumption
        except OctopusApiError as exc:
            if exc.status_code != 404 or not account_number:
                raise

            print(
                "Octopus did not find the configured gas meter. "
                "Trying gas meters from the Octopus account endpoint..."
            )

    if not account_number:
        raise RuntimeError(
            "Set OCTOPUS_MPRN and OCTOPUS_GAS_SERIAL, or set OCTOPUS_ACCOUNT_NUMBER "
            "so the gas meter can be discovered automatically."
        )

    candidates = get_gas_meter_candidates(get_octopus_account(api_key, account_number))
    if not candidates:
        raise RuntimeError("No gas meters were found on the Octopus account.")

    last_error = None
    for candidate in candidates:
        try:
            total_consumption = get_meter_consumption_from_octopus(
                api_key,
                candidate["mprn"],
                candidate["gas_serial_number"],
                initial_meter_reading,
            )
            print(
                f"{describe_gas_meter_candidates(candidates)} Using discovered gas meter."
            )
            print(f"Total consumption is {total_consumption}")
            return total_consumption
        except OctopusApiError as exc:
            last_error = exc
            if exc.status_code != 404:
                raise

    raise RuntimeError(
        f"None of the gas meters on the Octopus account returned consumption data. "
        f"{describe_gas_meter_candidates(candidates)} Last error: {last_error}"
    )


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def activation_status_matches(status, expected):
    """
    python-tado returns a DeviceActivationStatus enum, but this also supports
    older string-like values if the dependency changes again.
    """
    return (
        status == expected
        or status == expected.value
        or getattr(status, "value", None) == expected.value
    )


def activation_status_label(status):
    return getattr(status, "value", str(status))


async def browser_login(url, username, password, headless=True):
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except Exception as exc:
        raise RuntimeError(
            "Playwright browser automation is unavailable. Install Playwright's "
            "browser dependencies, or rerun with --manual-tado-login and open "
            "the Tado verification URL yourself."
        ) from exc

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            slow_mo=0 if headless else 250,
        )
        try:
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto(url)

            try:
                await page.wait_for_selector('text="Submit"', timeout=5000)
                await page.click('text="Submit"')
            except PlaywrightTimeoutError:
                pass

            # Wait for the login form to appear
            await page.wait_for_selector('input[name="loginId"]')

            await page.fill('input[id="loginId"]', username)
            await page.fill('input[name="password"]', password)

            await page.click('button.c-btn--primary:has-text("Sign in")')

            await page.wait_for_selector(
                ".text-center.message-screen.b-bubble-screen__spaced", timeout=10000
            )

            if env_flag("TADO_DEBUG_SCREENSHOTS"):
                await page.screenshot(path="after-message.png")

        finally:
            await browser.close()


def complete_tado_login_manually(url):
    print(f"Open this Tado verification URL and complete login:\n{url}")
    input("Press Enter once Tado says the device login is complete...")


def tado_login(
    username,
    password,
    token_file_path=DEFAULT_TADO_TOKEN_FILE,
    manual_login=False,
    browser_headless=True,
):
    tado = Tado(token_file_path=token_file_path)

    status = tado.device_activation_status()

    if activation_status_matches(status, DeviceActivationStatus.PENDING):
        url = tado.device_verification_url()
        if not url:
            raise RuntimeError(
                "Tado device activation is pending, but no verification URL was returned."
            )

        if manual_login:
            complete_tado_login_manually(url)
        else:
            asyncio.run(
                browser_login(url, username, password, headless=browser_headless)
            )

        tado.device_activation()

        status = tado.device_activation_status()

    if not activation_status_matches(status, DeviceActivationStatus.COMPLETED):
        raise RuntimeError(
            f"Tado login failed. Activation status is {activation_status_label(status)}."
        )

    print("Login successful")

    return tado


def send_reading_to_tado(
    username,
    password,
    reading,
    token_file_path=DEFAULT_TADO_TOKEN_FILE,
    manual_login=False,
    browser_headless=True,
):
    """
    Sends the total consumption reading to Tado using its Energy IQ feature.
    """

    tado = tado_login(
        username=username,
        password=password,
        token_file_path=token_file_path,
        manual_login=manual_login,
        browser_headless=browser_headless,
    )

    result = tado.set_eiq_meter_readings(
        date=date.today().isoformat(), reading=int(reading)
    )
    print(result)


def parse_args():
    """
    Parses command-line arguments for Tado and Octopus API credentials and meter details.
    """
    parser = argparse.ArgumentParser(
        description="Tado and Octopus API Interaction Script"
    )

    # Tado API arguments
    parser.add_argument("--tado-email", required=True, help="Tado account email")
    parser.add_argument("--tado-password", required=True, help="Tado account password")
    parser.add_argument(
        "--tado-token-file",
        default=os.environ.get("TADO_TOKEN_FILE", DEFAULT_TADO_TOKEN_FILE),
        help="Path used to persist the Tado refresh token",
    )
    parser.add_argument(
        "--manual-tado-login",
        action="store_true",
        default=env_flag("TADO_MANUAL_LOGIN"),
        help="Print the Tado verification URL and wait for manual login instead of using Playwright",
    )
    parser.add_argument(
        "--tado-browser-headless",
        action=argparse.BooleanOptionalAction,
        default=env_flag("TADO_BROWSER_HEADLESS", default=env_flag("CI")),
        help="Run the Playwright login browser in headless mode",
    )

    # Octopus API arguments
    parser.add_argument(
        "--mprn",
        default=os.environ.get("OCTOPUS_MPRN"),
        help="MPRN (Meter Point Reference Number) for the gas meter",
    )
    parser.add_argument(
        "--gas-serial-number",
        default=os.environ.get("OCTOPUS_GAS_SERIAL"),
        help="Gas meter serial number",
    )
    parser.add_argument("--octopus-api-key", required=True, help="Octopus API key")
    parser.add_argument(
        "--octopus-account-number",
        default=os.environ.get("OCTOPUS_ACCOUNT_NUMBER"),
        help="Octopus account number, used to auto-discover gas meter details",
    )
    parser.add_argument(
        "--initial-meter-reading",
        type=float,
        default=float(
            os.environ.get("OCTOPUS_INITIAL_READING", DEFAULT_INITIAL_METER_READING)
        ),
        help="Meter reading at the start of the Octopus consumption history",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # Get total consumption from Octopus Energy API
    consumption = get_meter_reading_total_consumption(
        args.octopus_api_key,
        args.mprn,
        args.gas_serial_number,
        account_number=args.octopus_account_number,
        initial_meter_reading=args.initial_meter_reading,
    )

    # Send the total consumption to Tado
    send_reading_to_tado(
        args.tado_email,
        args.tado_password,
        consumption,
        token_file_path=args.tado_token_file,
        manual_login=args.manual_tado_login,
        browser_headless=args.tado_browser_headless,
    )
