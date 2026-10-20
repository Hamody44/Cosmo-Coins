# Security notes and deployment checklist

## Required deployment settings

- Keep credentials in an untracked `.env` file or a secret manager. Start from `.env.example` and set a strong, stable `FLASK_SECRET_KEY` (at least 32 characters). If the key is omitted, the dashboard generates an unpredictable process-local key; users will need to sign in again after a restart.
- Keep `SESSION_COOKIE_SECURE=true` when serving the dashboard over HTTPS. Set it to `false` only for local HTTP development.
- Do not commit Discord, AI-provider, Cloudflare Tunnel, or ngrok credentials, database files, or generated backups.
- For Litecoin top-ups, store `NOWPAYMENTS_API_KEY` and `NOWPAYMENTS_IPN_SECRET` only in the host's secret manager, and set `DASHBOARD_URL` to the public HTTPS dashboard base so `/nowpayments/ipn` is reachable by NOWPayments.

## NOWPayments LTC top-ups

- Set `NOWPAYMENTS_PAYOUT_ADDRESS` to the owner's Litecoin wallet (`ltc1qrn9vg3dcz7wuq6auf5xw4swzmuuzuu3wleu23m`) and keep `NOWPAYMENTS_PAYOUT_CURRENCY=ltc`. The API receives that payout destination; NOWPayments returns a distinct deposit address for each order, which is the address the member must pay.
- The bot checks the live provider minimum and USD-to-LTC estimate, creates a fixed-rate payment, and uses the returned LTC amount/address. A fixed quote is valid for 10 minutes. `NOWPAYMENTS_FEE_PAID_BY_USER=false` preserves the displayed quote, with provider fees deducted from merchant proceeds; set it to `true` only if the owner wants the buyer to cover provider fees.
- `/nowpayments/ipn` is the only POST route exempt from form CSRF because every request must pass NOWPayments HMAC-SHA512 signature validation. Budget credit is atomic and idempotent and requires a matching provider order/payment ID, USD quote, LTC pay-in/network and LTC payout, expected deposit address and amount, exact actual payment, full `finished` status, and no repeated-deposit parent ID. Partial, overpaid, wrong-asset, or repeated payments are left for manual review.
- Personal Cosmo-to-server-budget transfers debit the global balance and add the guild budget in one attached-database transaction. Keep both SQLite files in rollback-journal mode (not WAL); the transfer intentionally fails closed if either database is in WAL mode.

## Previously published ngrok credential

A credential was present in the public GitHub history before this security fix. Removing it from the current tree does **not** invalidate it and does not erase older commits. Revoke/rotate the credential in the ngrok account immediately. History rewriting and force-pushing require a separate coordinated operation because existing clones and open work based on the public history will be affected.

## Security changes in this release

- The dashboard verifies the OAuth user's actual guild owner/manage-guild/administrator permission for every `/manage/<guild_id>` request and verifies that the bot is present in that guild.
- Dashboard POST forms require a session-bound CSRF token; Discord OAuth uses a single-use `state` value.
- The dashboard no longer uses a known fixed Flask signing key. Session cookies are HTTP-only, `SameSite=Lax`, and secure by default.
- User-provided avatar/banner URLs are restricted to HTTPS, globally routable resolved IPs, non-redirecting responses, image MIME types, and an 8 MiB size limit.
- Transfers, exchange debits, acceptance, and rejection/refunds use conditional SQL updates and transactions so concurrent requests cannot spend or refund the same balance twice.
- NOWPayments IPNs are authenticated with HMAC-SHA512; only full, matched Litecoin payments can add server budget, and the resulting credit is written to `server_budget_history`.
- Cross-database personal Cosmo contributions use `BEGIN IMMEDIATE`, rollback-journal checks, and a single transaction covering both the debit and budget-history entry.
- Runtime state and local secrets are excluded by `.gitignore`.
