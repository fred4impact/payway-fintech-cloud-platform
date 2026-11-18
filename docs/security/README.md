# Security

Controls implemented in the application (`application/django-source`):

| Area | Control |
|---|---|
| Authentication | Email/password sign-in; optional TOTP two-factor (authenticator app) with one-time backup codes. 2FA is only enabled after a valid code is verified. |
| Account protection | Failed-login tracking and lockout, device tracking, security event log with risk scores (`userauths.SecurityLog`). |
| Password changes | Require the current password and pass Django's password validators. |
| Card data | Card numbers and CVVs encrypted at rest with Fernet using `ENCRYPTION_KEY` (set it in production; the development fallback is derived from `SECRET_KEY`). Only the last four digits are displayed. |
| Money movement | Review-then-confirm flows; balance checks and updates run inside DB transactions with row locks (`select_for_update`) to prevent double-spending. |
| Transport & browser | nginx Content-Security-Policy restricted to `'self'` (all front-end assets are self-hosted), security headers, rate limiting on auth endpoints. |
| Pipeline | Bandit (SAST), Safety and pip-audit (SCA), Trivy image scanning — see `.github/workflows/`. |

`vulnerable-requirements-reference.md` keeps the earlier dependency pins that the SCA stage flagged, for comparison.
