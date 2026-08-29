# PR Party reviewer credential rewrap

This operation does **not** replace or revoke a GitHub PAT. It re-encrypts each stored PR Party reviewer PAT under the current `SECRET_KEY` while the retired key remains available through `SECRET_KEY_PREVIOUS`.

1. Put the new current key in `SECRET_KEY`, keep the retired key in `SECRET_KEY_PREVIOUS`, and recreate every API and worker replica. Never place either value in a command line, log, issue, pull request, or Git. Verify all replicas use the same deployment generation before either command runs; mixed current/previous key order can rotate in the wrong direction while still producing plausible counts.
2. Run the operator command in dry-run mode from the deployed worker image:

   ```sh
   docker compose exec -T worker python /usr/local/bin/rewrap-pr-party-credentials
   ```

3. Confirm the JSON receipt reports equal `credentials_scanned` and `credentials_verified` counts and only expected credential-row UUIDs. The receipt deliberately contains no plaintext or ciphertext.
4. Apply the same proven batch with the explicit confirmation phrase:

   ```sh
   docker compose exec -T worker python /usr/local/bin/rewrap-pr-party-credentials \
     --apply --confirm REWRAP-PR-PARTY-CREDENTIALS
   ```

The ARQ task locks all credential rows, prepares and verifies every rotated ciphertext under the current key before changing any row, and commits once. A single corrupt or unreadable row rolls back the whole batch. Dry-run and apply use separate stable job IDs; an overlapping or recently retained duplicate is refused rather than executed twice. Worker logs and command results contain only the safe receipt.

This receipt proves only the PR Party credential domain. `SECRET_KEY_PREVIOUS` is shared with other encrypted domains, including stored LLM provider keys. Do not remove a retired key globally until those domains have been separately inventoried, rewrapped, and verified under the current key. After all domains are proven, remove the retired value, recreate every credential consumer, and run the relevant read-path smoke checks.

Do not run this procedure merely to validate the code. Unit and disposable-database integration tests are the verification path until an application-key rotation is explicitly scheduled.
