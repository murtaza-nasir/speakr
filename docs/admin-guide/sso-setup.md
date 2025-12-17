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

- If `SSO_AUTO_REGISTER=true`, new users are created on first login when their email domain is allowed (or when allowlist is empty).
- If `SSO_AUTO_REGISTER=false`, only existing users with a linked SSO subject can sign in.
- Email domain allowlist is enforced only when an email is present.

## Disabling password login

Set `SSO_DISABLE_PASSWORD_LOGIN=true` to enforce SSO-only authentication for regular users. When enabled:

- The login page shows only the SSO sign-in button
- Regular users cannot log in with email/password
- **Administrators can still use password login** as a fallback (hidden behind "Administrator login" link)

This is useful for organizations that want to enforce SSO for all users while keeping emergency admin access available.

## Security note

When a user logs in via SSO with an email that matches an existing Speakr account, the accounts are automatically linked. This is convenient for most setups but relies on trusting your IdP to provide accurate email information.

For self-hosted deployments where you control both Speakr and the IdP, this is generally not a concern. If you're using an IdP where users can set unverified email addresses, be aware that this could allow account linking without email ownership verification. Consider using `SSO_ALLOWED_DOMAINS` to restrict which email domains can authenticate.

## Linking existing users

- In **Account > Single Sign-On**, click **Link {PROVIDER} account** while logged in.
- If the SSO subject is already linked to another user, the link is rejected.

## Unlinking SSO

Users can unlink their SSO account from **Account > Single Sign-On** by clicking **Unlink {PROVIDER} account**. This removes the SSO association while keeping the local account intact.

**Important:** Users who created their account via SSO (and have no password set) must first set a password before unlinking. Otherwise they would be locked out of their account.

## Troubleshooting

- **Login fails immediately**: verify `SSO_DISCOVERY_URL`, client credentials, and that the redirect URI matches exactly.
- **User created without email**: some IdPs do not return `email`; user is created with a placeholder email based on `sub`.
- **Domain rejected**: confirm `SSO_ALLOWED_DOMAINS` and that the IdP returns an `email` claim.
- **Already linked**: ensure each SSO subject is unique; users can unlink from Account settings to re-link to a different account.

