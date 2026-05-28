# Octopus Energy & Tado Energy IQ Sync Tool

This repository contains a script to automatically sync your Octopus Energy
smart meter readings with Tado's Energy IQ feature. The workflow provided allows
you to set this up to run on a weekly basis using GitHub Actions, so your Tado
Energy IQ remains up-to-date without any manual effort.

**Note**: This tool is specifically oriented toward users with smart gas meters
from Octopus Energy.

## Features

- Automatically pulls your gas usage data from Octopus Energy.
- Syncs the data with Tado Energy IQ for better home energy management insights.
- Set up once, and it runs weekly via GitHub Actions.

## Setup Instructions

Follow these steps to configure the sync for your own Octopus Energy and Tado
accounts:

### 1. Fork This Repository

First, fork this repository to your own GitHub account. This will allow you to
customize the secrets specific to your accounts and run the workflow
independently.

### 2. Configure GitHub Secrets

In order to use the script, you'll need to provide credentials for both your
Tado account and Octopus Energy API. This is done through GitHub secrets.

The workflow uses a GitHub environment named `Secrets`. If you create
environment secrets instead of repository secrets, make sure they are added to an
environment with that exact name.

1. Go to the **Settings** tab of your forked repository.
2. In the left-hand menu, select **Secrets and variables** > **Actions**.
3. Click **New repository secret** and add the following secrets:

| Secret Name              | Description                                                   |
|--------------------------|---------------------------------------------------------------|
| `TADO_EMAIL`             | The email address associated with your Tado account.          |
| `TADO_PASSWORD`          | The password for your Tado account.                           |
| `OCTOPUS_MPRN`           | Your gas MPRN (Meter Point Reference Number).                 |
| `OCTOPUS_GAS_SERIAL`     | The serial number of your gas meter.                          |
| `OCTOPUS_API_KEY`        | Your Octopus Energy API key. You can obtain this from the Octopus Energy developer portal (details below). |
| `OCTOPUS_ACCOUNT_NUMBER` | Optional but recommended. Used to auto-discover gas meter details if `OCTOPUS_MPRN` or `OCTOPUS_GAS_SERIAL` are wrong or missing. |
| `TADO_TOKEN_FILE`         | Optional path used to persist the Tado refresh token. Defaults to `/tmp/tado_refresh_token`. |
| `OCTOPUS_INITIAL_READING` | Optional meter reading at the start of the Octopus consumption history. Defaults to `6537.9`. |

### 3. Obtain Your Octopus Energy Details

To find your **API Key**, **Account Number**, **Gas MPRN**, and **Gas Serial
Number**, follow these steps:

1. Log into your [Octopus Energy
account](https://octopus.energy/dashboard/new/accounts/personal-details/api-access).
2. Navigate to the "API Access" section of your account. Here, you'll find your
**API Key**.
3. Your **Account Number**, **Gas MPRN**, and **Gas Serial Number** can also be found in this
section.

These details are necessary to allow the script to pull your gas usage data from
Octopus Energy.

### 4. Enable the Workflow

The repository is already set up with a GitHub Actions workflow that runs the
sync script once a week. The workflow is located at
`.github/workflows/schedule_sync.yml`. After you’ve added your secrets, the
workflow will automatically begin running on schedule.

You can manually trigger the workflow by navigating to the **Actions** tab in
your repository and selecting the sync workflow.

### 5. (Optional) Customize the Schedule

By default, the workflow runs weekly. If you want to change the schedule:

1. Open the `.github/workflows/schedule_sync.yml` file in your repository.
2. Modify the schedule trigger under `on: schedule:` following [the cron
syntax](https://crontab.guru/) for the desired frequency.

For example, to run daily at midnight:

```yaml
on: schedule:
    - cron: '0 0 * * *' ```
```

### 6. Monitor the Workflow

You can check the status of the sync runs in the **Actions** tab of your GitHub
repository. Here, you can see past runs, their logs, and any errors that might
have occurred.

### Usage

The GitHub Actions workflow automatically runs the following script:

```bash
python sync_octopus_tado.py \
  --tado-email "${{ secrets.TADO_EMAIL }}" \
  --tado-password "${{ secrets.TADO_PASSWORD }}" \
  --mprn "${{ secrets.OCTOPUS_MPRN }}" \
  --gas-serial-number "${{ secrets.OCTOPUS_GAS_SERIAL }}" \
  --octopus-api-key "${{ secrets.OCTOPUS_API_KEY }}" \
  --octopus-account-number "${{ secrets.OCTOPUS_ACCOUNT_NUMBER }}"
```

For local runs where browser automation is blocked by security software, use
manual Tado activation:

```bash
python sync_octopus_tado.py \
  --manual-tado-login \
  --tado-email "you@example.com" \
  --tado-password "your-password" \
  --mprn "your-mprn" \
  --gas-serial-number "your-meter-serial" \
  --octopus-api-key "your-octopus-api-key" \
  --octopus-account-number "your-account-number" \
  --initial-meter-reading "your-starting-meter-reading"
```

The script will:

1. Fetch the most recent gas usage readings from your Octopus Energy account
using their API.
2. Sync these readings with Tado's Energy IQ to keep your gas consumption
insights up-to-date.

### Troubleshooting

- **Incorrect credentials**: If the script fails due to incorrect credentials,
  ensure that the email, password, MPRN, and serial number are accurate. Verify
that your Octopus API key is valid.
- **Workflow failures**: Detailed logs for each sync run can be found in the
  **Actions** tab of your repository. Use these logs to identify and
troubleshoot any issues.
- **Secrets are empty in GitHub Actions debug logs**: If debug output shows
  `secrets.NAME => null`, add the missing values under **Settings** > **Secrets
  and variables** > **Actions** for the repository running the workflow. GitHub
  does not provide repository secrets to pull requests from forks.
- **Octopus 404 errors**: Add `OCTOPUS_ACCOUNT_NUMBER` as a GitHub secret so the
  script can discover your gas meter details from the Octopus account endpoint.
  If it still fails, verify that the account has a gas meter and the API key is
  for that account.
- **Browser or antivirus blocks**: The script can complete the Tado device login
  without launching Playwright by passing `--manual-tado-login`. The scheduled
  GitHub workflow runs Playwright headlessly.

### Contributions

Feel free to contribute to this project by opening issues or submitting pull
requests. Any improvements, bug fixes, or new feature suggestions are welcome!

### License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file
for details.
