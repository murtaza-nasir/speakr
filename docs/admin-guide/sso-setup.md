# SSO Setup (OIDC)

This guide explains how to enable Single Sign-On (SSO) for Speakr using any OpenID Connect (OIDC) identity provider such as Keycloak, Azure AD/Entra ID, Google, or Auth0.

## Prerequisites

- Speakr server reachable by the IdP at the redirect URL you configure.
- Client ID and Client Secret issued by your IdP.
- OIDC discovery (well-known) URL from your IdP.

## Required environment variables

Set these variables (see `config/env.sso.example`):

```
ENABLE_SSO=true
SSO_PROVIDER_NAME=Keycloak
SSO_CLIENT_ID=speakr
SSO_CLIENT_SECRET=change-me
SSO_DISCOVERY_URL=https://keycloak.example.com/realms/master/.well-known/openid-configuration
SSO_REDIRECT_URI=https://speakr.example.com/auth/sso/callback

# Auto-registration (email domain filter)
SSO_AUTO_REGISTER=true
SSO_ALLOWED_DOMAINS=example.com,company.org

# Disable password login for regular users (optional)
SSO_DISABLE_PASSWORD_LOGIN=false

# Claim mapping (optional)
SSO_DEFAULT_USERNAME_CLAIM=preferred_username
SSO_DEFAULT_NAME_CLAIM=name
```

Restart Speakr after updating environment variables.

## Claim expectations

- `sub` (required): stable subject identifier.
- `email` (recommended): used for matching and domain allowlist.
- `preferred_username` or `name`: used for username/full name if provided.

The claims may live in the ID token or at the UserInfo endpoint; both work. When the ID token carries only the bare claims (`sub`, `iss`, `aud`), as Keycloak, Azure AD, and LemonLDAP::NG do by default, Speakr fetches the UserInfo endpoint automatically and merges its claims, verifying the endpoint's `sub` matches the ID token's. No mapper configuration is needed on the IdP side to embed claims in the ID token.

## Keycloak quick start

1. In Keycloak, create a new client (e.g., `speakr`) with:
    - **Client Type**: OpenID Connect
    - **Access Type**: Confidential
    - **Valid Redirect URI**: `https://your-host/auth/sso/callback`
    - **Web Origins**: `+` (or your domain)
2. Copy the **Client ID** and **Client Secret**.
3. Note the **OpenID Endpoint Configuration** (discovery) URL, typically:
    `https://<host>/realms/<realm>/.well-known/openid-configuration`

4. Set the environment variables accordingly and restart Speakr.

## Azure AD / Entra ID quick start

1. Create an App Registration.
2. Add a **Web Redirect URI**: `https://your-host/auth/sso/callback`.
3. Grant API permissions: `openid`, `profile`, `email`.
4. Create a client secret.
5. Discovery URL format:
    `https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration`

6. Set variables and restart.

## Google quick start

1. Create OAuth credentials (Web application).
2. Add authorized redirect URI: `https://your-host/auth/sso/callback`.
3. Use discovery URL:
    `https://accounts.google.com/.well-known/openid-configuration`

4. Set variables and restart.

## Auth0 quick start

1. Create a Regular Web Application.
2. Allowed Callback URLs: `https://your-host/auth/sso/callback`.
3. Discovery URL:
    `https://<your-tenant>.auth0.com/.well-known/openid-configuration`

4. Set variables and restart.

## Auto-registration behavior

- If `SSO_AUTO_REGISTER=true`, new users are created on first sign-in when their email domain is allowed (or when allowlist is empty).
- If `SSO_AUTO_REGISTER=false`, only existing users with a linked SSO subject can sign in.
- Email domain allowlist is enforced only when an email is present.

## Staying signed in

A **Remember me** option is shown below the SSO button on the sign-in page. When it is ticked, the sign-in lasts `REMEMBER_COOKIE_DAYS` days (default 30); otherwise it ends when the browser is closed. Before v0.10.15, every SSO sign-in was remembered for a year.

## Disabling password login

Set `SSO_DISABLE_PASSWORD_LOGIN=true` to enforce SSO-only authentication for regular users. When enabled:

- The sign-in page shows only the SSO sign-in button
- Regular users cannot sign in with email/password
- **Administrators can still use password sign-in** as a fallback (behind the "Administrator sign-in" link)

This is useful for organizations that want to enforce SSO for all users while keeping emergency admin access available.

## Security note

When a user logs in via SSO with an email that matches an existing Speakr account, the accounts are automatically linked. This is convenient for most setups but relies on trusting your IdP to provide accurate email information.

For self-hosted deployments where you control both Speakr and the IdP, this is generally not a concern. If you're using an IdP where users can set unverified email addresses, be aware that this could allow account linking without email ownership verification.

This is closed off by default: `SSO_REQUIRE_VERIFIED_EMAIL` defaults to `true`. An SSO login whose `email_verified` claim is not true is refused before the email is used to link to (or provision) a local account, so a malicious or misconfigured IdP cannot take over an account by asserting its address. The claim is accepted as either a boolean or the string `"true"`. Users already linked by SSO subject are unaffected and keep signing in even if the IdP omits the claim.

If your IdP does not send an `email_verified` claim and you trust it, set `SSO_REQUIRE_VERIFIED_EMAIL=false` to restore the previous behavior; otherwise all email-bearing logins from that IdP are rejected. When you opt out this way, a warning is logged at startup so the relaxed setting stays visible. You can also use `SSO_ALLOWED_DOMAINS` to restrict which email domains can authenticate.

> **Changed in v0.10.2-alpha:** `SSO_REQUIRE_VERIFIED_EMAIL` now defaults to `true` (previously `false`). Deployments whose IdP does not send `email_verified` must set it to `false` to keep the old behavior.

## Linking existing users

- In **Account > Single Sign-On**, click **Link {PROVIDER} account** while logged in.
- If the SSO subject is already linked to another user, the link is rejected.

## Unlinking SSO

Users can unlink their SSO account from **Account > Single Sign-On** by clicking **Unlink {PROVIDER} account**. This removes the SSO association while keeping the local account intact.

**Important:** Users who created their account via SSO (and have no password set) must first set a password before unlinking. Otherwise they would be locked out of their account.

## API Access With SSO Access Tokens

Apps that sign users in through the same OIDC provider as Speakr can call the Speakr API for a user with that user's access token from the provider. The user then has no Speakr API token to copy. Two uses are supported:

- A tool server connection that sends each user's own access token on every call, such as Open WebUI with the **System OAuth** connection type.
- An app that works in the background, such as a mail client that syncs on a schedule. A short-lived access token is not enough there, so the app exchanges the access token once for a normal Speakr API token at `POST /api/v1/tokens/exchange` and stores that token.

The feature is off by default. Six settings control it. Each can be set in **System Settings** or in `.env` (see [Settings That Fall Back to .env](system-settings.md#settings-that-fall-back-to-env)), and a value saved in System Settings takes effect without a restart.

| Setting | Variable in `.env` | Default | Meaning |
|---|---|---|---|
| `sso_api_tokens_enabled` | `SSO_API_TOKENS_ENABLED` | `false` | Turns the feature on. SSO must also be configured (`ENABLE_SSO=true` and `SSO_DISCOVERY_URL`). |
| `sso_api_allowed_clients` | `SSO_API_ALLOWED_CLIENTS` | empty | Comma-separated OIDC client IDs of the apps whose access tokens are accepted. How the client of a token is found is described under [How an access token is checked](#how-an-access-token-is-checked). Empty means that no access token is accepted. |
| `sso_api_exchange_clients` | `SSO_API_EXCHANGE_CLIENTS` | empty | Comma-separated OIDC client IDs whose access tokens may be exchanged for API tokens. A client must also be in `sso_api_allowed_clients`. Empty means that no exchange is allowed, and an exchange request gets `403` with `"code": "sso_exchange_not_allowed"`. Add only apps that sync in the background. |
| `sso_api_scopes` | `SSO_API_SCOPES` | `read` | Comma-separated [scopes](../user-guide/api-tokens.md#scopes) that a request with an SSO access token has. A token created through the exchange may have these scopes and no others. `full` is not accepted here, and unknown names are ignored. |
| `sso_api_client_names` | `SSO_API_CLIENT_NAMES` | empty | Display names for client IDs, as `client_id=Name` pairs separated by commas or new lines, for example `abc123=Open WebUI`. The name is shown in **Account settings > API tokens** for a token created through the exchange. Malformed pairs are ignored, and a name is cut to 60 characters. |
| `sso_api_client_scopes` | `SSO_API_CLIENT_SCOPES` | empty | Scope limits per client ID, as `client_id=scope1 scope2` pairs separated by commas or new lines. The scopes of a pair are separated by spaces, `+` or `\|`. See [Scopes per app](#scopes-per-app). |

`sso_api_scopes` applies to every allowed client that `sso_api_client_scopes` does not list. Give it the largest set that any of those apps needs.

A tool server such as Open WebUI belongs in `sso_api_allowed_clients` only, never in `sso_api_exchange_clients`. A model that calls tools on a user's behalf can then never create a stored token, even when text in a recording tries to make it do so. For the same reason, `POST /api/v1/tokens/exchange` is not listed in `/api/v1/openapi.json`, so a tool list built from that document does not contain it.

### Scopes per app

With `sso_api_client_scopes`, the scopes of single apps are narrowed. A listed client gets the scopes of its pair that are also in `sso_api_scopes`, so `sso_api_scopes` stays the outer limit for every app. A client that is not listed keeps `sso_api_scopes`. The limit applies to each request made with an access token of that app, and to the exchange: the scopes requested there must be within the limit of the exchanging client, and a token from a request without `scopes` is given every scope within that limit.

Unknown scope names are ignored, and `full` is never granted. A pair without `=` or without a client ID is ignored. A listed client that ends up with no scope, for example `abc123=` or `abc123=bogus`, is refused: its access tokens get the same `401` as an invalid token, and an exchange for it gets `400` with `"allowed_scopes": []`.

In the following example, an assistant tool server such as Open WebUI (client ID `owui-client`) is limited to `read`, and a mail app that syncs in the background (client ID `mail-client`) is given `read`, `write`, `upload`, `share` and `webhooks`. In `.env`:

```
SSO_API_SCOPES=read,write,upload,share,webhooks
SSO_API_CLIENT_SCOPES="owui-client=read, mail-client=read write upload share webhooks"
```

In **System Settings**, the same value is entered without the quotation marks.

The same pairs may also be written with `+` or `|`, for example `mail-client=read+write+upload`.

### Requirements for the provider

- Access tokens must be signed JWTs. Opaque access tokens, which only the provider can look up, are not supported, because no token introspection endpoint is called. Pocket ID, Keycloak and Authentik issue JWT access tokens. Google issues opaque access tokens and cannot be used for this.
- Pocket ID issues access tokens in the JWT profile of RFC 9068: the header has `typ: at+jwt`, the `client_id` claim holds the client ID of the app, and `aud` holds the granted audience, which is often only the issuer. Such tokens are matched by `client_id`, so Pocket ID works without any audience setting.
- The signing algorithm must be asymmetric: RS256, RS384, RS512, PS256, PS384, PS512, ES256, ES384, ES512 or EdDSA. A token signed with HS256 or another shared-secret algorithm, or not signed at all, is refused.
- The discovery document at `SSO_DISCOVERY_URL` must name the `issuer` and the `jwks_uri`.

No new client has to be registered in the provider for Speakr. The client IDs to allow are those of the other apps.

### How an access token is checked

Every check must pass:

1. The signature is verified with a key from the `jwks_uri` in the discovery document. The discovery document and the key set are kept for an hour. When a token names a key ID (`kid`) that the kept key set lacks, as happens after the provider rotates its keys, the key set is fetched again, at most once every 30 seconds. No address taken from the token itself is ever fetched.
2. `iss` equals the issuer in the discovery document.
3. `exp` is in the future, and `nbf` and `iat` are not in the future. A clock difference of up to 60 seconds is allowed.
4. The client of the token is in `sso_api_allowed_clients`. For a token whose header has `typ` set to `at+jwt` or `application/at+jwt` (RFC 9068), the client is the `client_id` claim, which is then required; `aud` and `azp` are not used. For any other JWT, the client is named by `aud` (a string or a list) or `azp`, and a token that carries `nonce` or `at_hash` is refused, because those claims mark an ID token.
5. `sub` equals the SSO subject of an existing, active Speakr account. The subject is stored when the user first signs in to Speakr with SSO or links SSO under **Account > Single Sign-On**. The email address is never used to find an account. When no account has that subject, the token is refused, as described in [Accounts that do not exist yet](#accounts-that-do-not-exist-yet).

A token that fails any check gets the same `401` answer as an invalid API token. The reason is written to the Speakr log at INFO level, without the token.

A request with an SSO access token is treated like a request with an API token that has the scopes in `sso_api_scopes`. The same scope checks and rate limits apply. The rate limits count per user, and the account budget is shared with the user's API tokens. Such a request does not count as a browser sign-in. The token is accepted only in the `Authorization: Bearer` header, never in `?token=`.

### Accounts that do not exist yet

A Speakr account is created only when a person signs in to Speakr with SSO, under the rules of `SSO_AUTO_REGISTER`. A valid access token for a person who has no Speakr account is refused. On the exchange, the refusal has its own code. On every other route, the answer is the same `401` as for an invalid token, and the reason is written to the log at INFO level.

| Status | `code` | Meaning |
|---|---|---|
| `403` | `sso_registration_closed` | No account has this subject. The person signs in to Speakr once with SSO. |
| `403` | `sso_exchange_not_allowed` | No account has this subject, and the client of the token may not exchange. |
| `409` | `sso_link_required` | The token carries a verified `email` claim, and an account with this email exists that is not linked to this subject. The person signs in to Speakr once with SSO, which links the account through the usual rules. |

The `email` claim of the token only selects between `sso_link_required` and `sso_registration_closed`. It never links an account and never grants access. A token without a verified `email` claim gets `sso_registration_closed`.

An app with no token yet reads at `GET /api/v1/sso/status`, which needs no authentication, whether the exchange is available (see [SSO Status](../user-guide/api-reference.md#sso-status)).

### Enabling it for Open WebUI

1. Make sure that Open WebUI and Speakr sign users in through the same provider, with the same issuer.
2. In the provider, look up the client ID of the Open WebUI client (the value of `OAUTH_CLIENT_ID` in Open WebUI).
3. In Speakr, open **System Settings** and set `sso_api_allowed_clients` to that client ID. Leave it out of `sso_api_exchange_clients`. Set `sso_api_scopes` to `read` for searching and reading, or to `read,process` if the model may also chat with a recording or start a summary. Then set `sso_api_tokens_enabled` to true.
4. In Open WebUI, add Speakr as a tool server with the **System OAuth** connection type, as described in [Open WebUI with SSO](../user-guide/api-reference.md#open-webui-with-sso).
5. Each user signs in to Speakr with SSO once, so that the account is created or linked.

### Enabling it for an app that syncs in the background

1. In the provider, look up the client ID of the app.
2. In Speakr, add that client ID to `sso_api_allowed_clients`, separated by a comma from any other client ID, and to `sso_api_exchange_clients`. Make sure that `sso_api_scopes` holds every scope the app asks for, for example `read,write,upload`. Set `sso_api_tokens_enabled` to true.
3. The app sends the user's access token once to `POST /api/v1/tokens/exchange` (see [Token Exchange](../user-guide/api-reference.md#token-exchange)) and stores the API token it receives.
4. Each user signs in to Speakr with SSO once, so that the account is created or linked.

A token created this way appears in the user's **Account settings > API tokens** with the note **Created through SSO by** and the name of the app from `sso_api_client_names`, or its client ID when no name is set. It counts toward the per-account token limit, and the user can revoke it there like any other token. Turning the feature off stops access with SSO access tokens and further exchanges. Tokens created earlier through the exchange keep working until they expire or are revoked.

## Troubleshooting

- **Login fails immediately**: verify `SSO_DISCOVERY_URL`, client credentials, and that the redirect URI matches exactly.
- **User created without email**: some IdPs do not return `email`; user is created with a placeholder email based on `sub`.
- **Domain rejected**: confirm `SSO_ALLOWED_DOMAINS` and that the IdP returns an `email` claim.
- **Already linked**: ensure each SSO subject is unique; users can unlink from Account settings to re-link to a different account.
- **An app gets 401 with an SSO access token**: the Speakr log names the failed check at INFO level (`SSO access token refused: ...`). The usual causes are a client ID missing from `sso_api_allowed_clients`, an opaque access token, a user who has never signed in to Speakr with SSO, or a different issuer for the app and for Speakr.

