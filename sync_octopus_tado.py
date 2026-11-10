import argparse
import os
import sys
from datetime import date, datetime
from urllib.parse import quote

import requests
from requests.auth import HTTPBasicAuth
from tado_auth import GitHubTokenStore, TadoAuthError, tado_session

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


def send_reading_to_tado(tado, reading):
    """
    Sends the total consumption reading to Tado using its Energy IQ feature.
    """

    tado.set_eiq_meter_readings(date=date.today().isoformat(), reading=int(reading))
    print("Tado reading submitted.")


def validate_args(args):
    missing = []

    if args.manual_tado_login and not args.auth_only:
        raise RuntimeError("Use --manual-tado-login with --auth-only for setup.")
    if args.auth_only:
        return
    if not optional_text(args.octopus_api_key):
        missing.append("OCTOPUS_API_KEY")
    if not optional_text(args.octopus_account_number) and (
        not optional_text(args.mprn) or not optional_text(args.gas_serial_number)
    ):
        missing.append(
            "OCTOPUS_ACCOUNT_NUMBER (or both OCTOPUS_MPRN and OCTOPUS_GAS_SERIAL)"
        )

    if missing:
        raise RuntimeError(
            "Missing required configuration: "
            + ", ".join(missing)
            + ". Configure these as GitHub Actions secrets or pass CLI arguments."
        )


def parse_args(argv=None):
    """
    Parses command-line arguments for Tado and Octopus API credentials and meter details.
    """
    parser = argparse.ArgumentParser(
        description="Tado and Octopus API Interaction Script"
    )

    parser.add_argument(
        "--auth-only",
        action="store_true",
        help="Check and save Tado authentication without reading or submitting meter data",
    )
    parser.add_argument(
        "--manual-tado-login",
        action="store_true",
        default=env_flag("TADO_MANUAL_LOGIN"),
        help="Open Tado approval in the local browser and save the token directly to GitHub",
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
    parser.add_argument(
        "--octopus-api-key",
        default=os.environ.get("OCTOPUS_API_KEY"),
        help="Octopus API key",
    )
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

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    validate_args(args)
    store = GitHubTokenStore(
        os.environ.get("GITHUB_REPOSITORY", "seanor1701/octopus-to-tado-sync"),
        require_writer=os.environ.get("GITHUB_ACTIONS") == "true",
    )
    if not args.auth_only:
        consumption = get_meter_reading_total_consumption(
            args.octopus_api_key,
            args.mprn,
            args.gas_serial_number,
            account_number=args.octopus_account_number,
            initial_meter_reading=args.initial_meter_reading,
        )
    with tado_session(
        refresh_token=(
            None if args.manual_tado_login else os.environ.get("TADO_REFRESH_TOKEN")
        ),
        interactive=args.manual_tado_login,
        persist=store,
    ) as tado:
        if args.auth_only:
            print("Tado authentication confirmed; refresh token saved in GitHub.")
        else:
            send_reading_to_tado(tado, consumption)


if __name__ == "__main__":
    try:
        main()
    except TadoAuthError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
