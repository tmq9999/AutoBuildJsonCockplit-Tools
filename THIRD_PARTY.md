# Auth source

The application imports (does not vendor or relicense) the user-selected repository
https://github.com/tmq9999/Check-Account-ChatGPT.git at commit
`791beb350370c191bbe8ddbe0b4a3fa0072620d9`.

No explicit license file was found in that revision. Check the author's permissions
before redistributing their source. The checkout is local and excluded from builds.

Only the authentication library is integrated. Its CLI, database, subscription
checker and output writer are not executed. Debug output is disabled. Optional
sibling SentinelVM imports are isolated; Sentinel metadata follows the pinned
upstream fallback/optional-VM behavior. Actual CAPTCHA/Turnstile pages, redirects,
or HTTP rejection responses return safe action/blocked errors. HTTP endpoints can
change; offline tests do not prove live login.

The initial web-login bootstrap is implemented locally following the supplied
provider → CSRF → sign-in capture. It replaces only the upstream homepage warm-up
and sign-in URL builder; email/password/TOTP still use the pinned auth methods.
UUIDs and cookies are fresh per attempt. Captured browser cookies/CSRF values are
never bundled or reused. The provider's authorize URL is checked before navigation.
