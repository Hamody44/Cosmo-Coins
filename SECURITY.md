# Security notes and deployment checklist

## Required deployment settings

- Keep credentials in an untracked `.env` file or a secret manager. Start from `.env.example` and set a strong, stable `FLASK_SECRET_KEY` (at least 32 characters). If the key is omitted, the dashboard generates an unpredictable process-local key; users will need to sign in again after a restart.
- Keep `SESSION_COOKIE_SECURE=true` when serving the dashboard over HTTPS. Set it to `false` only for local HTTP development.
- Do not commit Discord, AI-provider, Cloudflare Tunnel, or ngrok credentials, database files, or generated backups.
- For Litecoin top-ups, keep `LTCPAY_API_KEY` and `LTCPAY_WEBHOOK_SECRET` only in the host's secret manager. Configure `LTCPAY_URL=https://ltcpay.com`, `LTCPAY_STORE_ID`, and a public HTTPS `DASHBOARD_URL`; register a store webhook at `/ltcpay/webhook` for invoice processing, settled, expired, and invalid events.

## LTCPay Litecoin top-ups

- Configure an LTC wallet/node in the LTCPay store and enable the Litecoin payment method. The bot sends USD-denominated invoices restricted to on-chain LTC (`LTC-CHAIN`); LTCPay creates the invoice's deposit address and displays the required LTC amount at the invoice's rate. Funds are received to the wallet configured on the store, not through the bot or an exchange processor. A single static receiving address alone is not enough for LTCPay to derive unique invoice addresses; connect/create the wallet within LTCPay. Never put a wallet seed or private key in the bot's environment or source code.
- The bot's policy minimum is $0.10, equal to 26,000,000 Cosmo at 260,000,000 Cosmo per USD. This is a minimum set by the bot, not a guarantee that every LTCPay instance or Litecoin node can accept that amount; network fees, node configuration, exchange-rate source availability, and dust rules may affect very small payments. Verify the minimum on the deployed instance before opening the option to users.
- Register the store webhook to the public HTTPS endpoint `/ltcpay/webhook` and subscribe to `InvoiceProcessing`, `InvoiceSettled`, `InvoiceExpired`, and `InvoiceInvalid`. The endpoint validates `BTCPAY-SIG` as HMAC-SHA256 over the exact raw request body, then retrieves the invoice from the configured store API before settlement. It credits only a matching `Settled` USD invoice with exact paid amount, expected order metadata, and only `LTC-CHAIN`; partial, overpaid, late, manually marked, invalid, wrong-store, or mismatched invoices are not credited automatically.
- Use an LTCPay API key scoped to the single store with only invoice creation and viewing permissions. Do not expose the API key or webhook secret in Discord, logs, source control, or browser code.
- Personal Cosmo-to-server-budget transfers debit the global balance and add the guild budget in one attached-database transaction. Keep both SQLite files in rollback-journal mode (not WAL); the transfer intentionally fails closed if either database is in WAL mode.

## Previously published ngrok credential

A credential was present in the public GitHub history before this security fix. Removing it from the current tree does **not** invalidate it and does not erase older commits. Revoke/rotate the credential in the ngrok account immediately. History rewriting and force-pushing require a separate coordinated operation because existing clones and open work based on the public history will be affected.

## Security changes in this release

- The dashboard verifies the OAuth user's actual guild owner/manage-guild/administrator permission for every `/manage/<guild_id>` request and verifies that the bot is present in that guild.
- Dashboard POST forms require a session-bound CSRF token; Discord OAuth uses a single-use `state` value.
- The dashboard no longer uses a known fixed Flask signing key. Session cookies are HTTP-only, `SameSite=Lax`, and secure by default.
- User-provided avatar/banner URLs are restricted to HTTPS, globally routable resolved IPs, non-redirecting responses, image MIME types, and an 8 MiB size limit.
- Transfers, exchange debits, acceptance, and rejection/refunds use conditional SQL updates and transactions so concurrent requests cannot spend or refund the same balance twice.
- LTCPay webhooks are authenticated with HMAC-SHA256 over the raw request body; only fully verified Litecoin invoices can add server budget, and the resulting credit is written to `server_budget_history`.
- Cross-database personal Cosmo contributions use `BEGIN IMMEDIATE`, rollback-journal checks, and a single transaction covering both the debit and budget-history entry.
- Runtime state and local secrets are excluded by `.gitignore`.
