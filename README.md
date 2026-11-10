# Octopus Energy to Tado Energy IQ

Synchronize a gas meter's cumulative Octopus consumption with Tado Energy IQ.
The GitHub Actions workflow runs daily at 12:00 UTC (GitHub may delay schedules).
Python 3.12 and 3.14 are covered by the offline CI test suite.

## Why authentication used to work once and then fail

Tado returns a replacement refresh token when renewing authentication. Every
runner starts from the `TADO_REFRESH_TOKEN` environment secret. Previously the
replacement was written only to the temporary runner and discarded at job end,
so a later run reused the old token and failed with HTTP 400 / `NOT_STARTED`.

The workflow now verifies write access before consuming the token, saves its
replacement back to the encrypted secret before submitting a meter reading,
and saves again on exit if the library refreshes it during an API request.
If token storage fails, the job fails. Only token-storage writes are retried;
meter submissions and rejected authentication are never automatically retried.
Jobs sharing the `Secrets` environment are serialized without cancelling the
active job. Do not run a local token refresh while a scheduled run is active.

## One-time setup or recovery

Use a GitHub environment named **Secrets**. Keep its existing review and branch
protection rules. The scheduled workflow only executes on `main`.

1. Pause the sync workflow in GitHub Actions while configuring or recovering it.
2. Create a [fine-grained GitHub personal access token](https://github.com/settings/personal-access-tokens/new).
   Select your repository owner and **Only select repositories**, choosing only
   this repository. Grant **Environments: Read and write** (Metadata read access
   is added automatically). Do not grant Contents, Actions, or other write
   permissions. Set an expiry and renew it before expiry. GitHub requires this
   permission to update [environment secrets](https://docs.github.com/en/rest/actions/secrets#create-or-update-an-environment-secret);
   the workflow's standard `GITHUB_TOKEN` is not a secret-management credential.
3. Save that GitHub token as an environment secret named
   **TADO_TOKEN_STORE_TOKEN**, in the **Secrets** environment. This is a GitHub
   token, distinct from the Tado refresh token. Never commit either token.
4. Locally install the requirements with Python 3.12 or 3.14:

   ```bash
   python -m pip install -r requirements.txt
   ```

5. Sign the GitHub CLI into the repository owner's account. For this repository:

   ```bash
   gh auth switch --user seanor1701
   ```

6. From your local checkout, run this single authorization command:

   ```bash
   python sync_octopus_tado.py --auth-only --manual-tado-login
   ```

   It opens Tado's approval page in your browser without displaying the device
   URL or token. Approve the device and return to Terminal. The script waits
   for PyTado's completed status, saves the raw refresh token directly into the
   **Secrets** environment, and removes its private temporary file. It does not
   fetch Octopus data or submit a meter reading. No `/tmp/tado_refresh_token`
   copy/paste, clipboard command, or email/password is needed.

7. Re-enable the workflow and choose **Run workflow**, leaving **auth_only**
   checked. This checks Tado authentication and saves the next token without
   fetching consumption or submitting a reading. A second auth-only run verifies
   that the replacement token survives between runners.
8. Once both checks succeed, scheduled runs perform the normal sync. Uncheck
   **auth_only** for a manual run only when you intend to submit a real reading.

The previous `TADO_EMAIL` and `TADO_PASSWORD` secrets are no longer used and may
be removed separately. Device approval replaces the password-based browser
automation; Playwright, screenshots and HTML dumps are not used.

## Other secrets

| Secret in environment `Secrets` | Purpose |
| --- | --- |
| `TADO_REFRESH_TOKEN` | Created and updated automatically by the authorization command and workflow. Raw token; the PyTado JSON export is also accepted for migration. |
| `TADO_TOKEN_STORE_TOKEN` | Fine-grained GitHub token for this repository, Environments read/write only. |
| `OCTOPUS_API_KEY` | Octopus API key. |
| `OCTOPUS_MPRN` and `OCTOPUS_GAS_SERIAL` | Gas meter details, not electricity MPAN/meter details. |
| `OCTOPUS_ACCOUNT_NUMBER` | Optional account discovery when explicit meter details are absent or return 404. |
| `OCTOPUS_INITIAL_READING` | Optional starting meter-reading offset. Preserves the existing documented `6537.9` default. Check that it matches this meter and the available consumption history. |

## Failures and recovery

- **HTTP 400 / rejected refresh token:** confirm the replacement-token change is
  installed, then perform the local authorization command once. Repeatedly
  copying the same old token cannot repair a rejected token.
- **Token store permission failure:** use the repository owner's GitHub CLI
  account locally. In Actions, check `TADO_TOKEN_STORE_TOKEN` expiry and its
  repository selection / Environments write permission. Do not substitute a
  broad administrator token or publish token files in caches or artifacts.
- **Expired or denied device approval:** rerun the local auth-only command and
  approve the newly opened browser prompt promptly.
- **Octopus 404:** verify the gas MPRN/serial against your Octopus API account,
  or configure account discovery. API errors never return a baseline-only or
  partial reading. The existing cumulative-consumption calculation is retained.
- **An interrupted runner or failed secret update:** a network interruption
  after Tado rotates a token can still require fresh local authorization. The
  private temporary file is cleaned on normal and exception paths; hosted-runner
  disposal handles forced termination. No distributed transaction exists between
  Tado and GitHub.

## Offline verification

```bash
python -m unittest discover -v
python -m black --check sync_octopus_tado.py tado_auth.py tests
python -m pip check
```

Tests mock external OAuth, Tado home/meter APIs and GitHub secret storage,
including three consecutive runs with rotating tokens, rejected refreshes,
activation failures, persistence failures, cleanup, and auth-only operation.

## License

See [LICENSE](LICENSE).
