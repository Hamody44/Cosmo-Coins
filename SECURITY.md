# Security notes and deployment checklist

## Required deployment settings

- Keep credentials in an untracked `.env` file or a secret manager. Start from `.env.example` and set a strong, stable `FLASK_SECRET_KEY` (at least 32 characters). If the key is omitted, the dashboard generates an unpredictable process-local key; users will need to sign in again after a restart.
- Keep `SESSION_COOKIE_SECURE=true` when serving the dashboard over HTTPS. Set it to `false` only for local HTTP development.
- Do not commit Discord, AI-provider, Cloudflare Tunnel, or ngrok credentials, database files, or generated backups.

## Previously published ngrok credential

A credential was present in the public GitHub history before this security fix. Removing it from the current tree does **not** invalidate it and does not erase older commits. Revoke/rotate the credential in the ngrok account immediately. History rewriting and force-pushing require a separate coordinated operation because existing clones and open work based on the public history will be affected.

## Security changes in this release

- The dashboard verifies the OAuth user's actual guild owner/manage-guild/administrator permission for every `/manage/<guild_id>` request and verifies that the bot is present in that guild.
- Dashboard POST forms require a session-bound CSRF token; Discord OAuth uses a single-use `state` value.
- The dashboard no longer uses a known fixed Flask signing key. Session cookies are HTTP-only, `SameSite=Lax`, and secure by default.
- User-provided avatar/banner URLs are restricted to HTTPS, globally routable resolved IPs, non-redirecting responses, image MIME types, and an 8 MiB size limit.
- Transfers, exchange debits, acceptance, and rejection/refunds use conditional SQL updates and transactions so concurrent requests cannot spend or refund the same balance twice.
- Runtime state and local secrets are excluded by `.gitignore`.
