# Email Verification & Password Reset

This guide explains how to configure email functionality in Speakr, enabling email verification for new user registrations and password reset capabilities for all users.

## Overview

Email features in Speakr are completely opt-in. When configured, they provide:

- **Email Verification**: Require new users to verify their email address before accessing the system
- **Password Reset**: Allow users to reset forgotten passwords via email

Both features work independently of domain restrictions—you can use email verification even with open registration (`ALLOW_REGISTRATION=true`) and no domain restrictions.

## Prerequisites

- SMTP server credentials (Gmail, SendGrid, Mailgun, Amazon SES, or any SMTP provider)
- Speakr instance accessible via the URL you configure (for email links to work)

## Configuration

### Required Environment Variables

Set these variables in your `.env` file (see `config/env.email.example` for a complete template):

```bash
# Enable email features
ENABLE_EMAIL_VERIFICATION=true
REQUIRE_EMAIL_VERIFICATION=false

# SMTP Configuration
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password
SMTP_USE_TLS=true
SMTP_FROM_ADDRESS=noreply@yourdomain.com
SMTP_FROM_NAME=Speakr
```

Restart Speakr after updating environment variables.

### Configuration Options

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_EMAIL_VERIFICATION` | `false` | Enable email verification for new registrations |
| `REQUIRE_EMAIL_VERIFICATION` | `false` | Block login for unverified users (only works when verification is enabled) |
| `SMTP_HOST` | (none) | SMTP server hostname |
| `SMTP_PORT` | `587` | SMTP server port |
| `SMTP_USERNAME` | (none) | SMTP authentication username |
| `SMTP_PASSWORD` | (none) | SMTP authentication password |
| `SMTP_USE_TLS` | `true` | Use STARTTLS encryption (port 587) |
| `SMTP_USE_SSL` | `false` | Use SSL encryption (port 465) |
| `SMTP_FROM_ADDRESS` | `noreply@yourdomain.com` | Email address shown in "From" field |
| `SMTP_FROM_NAME` | `Speakr` | Display name shown alongside from address |

### Understanding the Two Verification Modes

**Soft Verification** (`ENABLE_EMAIL_VERIFICATION=true`, `REQUIRE_EMAIL_VERIFICATION=false`):

- New users receive a verification email after registration
- Users can log in immediately without verifying
- Useful for encouraging email verification without blocking access

**Strict Verification** (`ENABLE_EMAIL_VERIFICATION=true`, `REQUIRE_EMAIL_VERIFICATION=true`):

- New users receive a verification email after registration
- Users cannot log in until they verify their email
- Best for environments requiring confirmed email addresses

### Combining with Other Registration Settings

Email verification works seamlessly with other registration controls:

```bash
# Open registration with email verification
ALLOW_REGISTRATION=true
ENABLE_EMAIL_VERIFICATION=true
REQUIRE_EMAIL_VERIFICATION=true

# Domain-restricted registration with verification
ALLOW_REGISTRATION=true
REGISTRATION_ALLOWED_DOMAINS=company.com,subsidiary.org
ENABLE_EMAIL_VERIFICATION=true
REQUIRE_EMAIL_VERIFICATION=true

# Closed registration (admin creates accounts)
ALLOW_REGISTRATION=false
# Email verification not applicable - admin creates verified accounts
```

## Provider-Specific Setup

### Gmail

```bash
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password
```

**Important:** Use an [App Password](https://support.google.com/accounts/answer/185833), not your regular Gmail password. App Passwords are required when 2-factor authentication is enabled (recommended).

### SendGrid

```bash
SMTP_HOST=smtp.sendgrid.net
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USERNAME=apikey
SMTP_PASSWORD=your-sendgrid-api-key
```

### Mailgun

```bash
SMTP_HOST=smtp.mailgun.org
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USERNAME=postmaster@your-domain.mailgun.org
SMTP_PASSWORD=your-mailgun-password
```

### Amazon SES

```bash
SMTP_HOST=email-smtp.us-east-1.amazonaws.com
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USERNAME=your-ses-smtp-username
SMTP_PASSWORD=your-ses-smtp-password
```

### Microsoft 365 / Outlook

```bash
SMTP_HOST=smtp.office365.com
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USERNAME=your-email@yourdomain.com
SMTP_PASSWORD=your-password
```

### SSL vs TLS

- **Port 587 with TLS** (recommended): Set `SMTP_USE_TLS=true`, `SMTP_USE_SSL=false`
- **Port 465 with SSL**: Set `SMTP_USE_TLS=false`, `SMTP_USE_SSL=true`
- **Port 25 (unencrypted)**: Not recommended for security reasons

## User Experience

### Registration Flow (with verification enabled)

1. User fills out registration form
2. Account is created with `email_verified=false`
3. Verification email is sent automatically
4. User sees "Check your email" page with option to resend
5. User clicks verification link in email
6. Account is marked as verified
7. User can now log in (if `REQUIRE_EMAIL_VERIFICATION=true`)

### Password Reset Flow

1. User clicks "Forgot password?" on login page
2. User enters their email address
3. If account exists, reset email is sent (no indication if account doesn't exist for security)
4. User clicks reset link in email
5. User sets new password
6. User is redirected to login

### Token Expiry

- **Email verification links**: Valid for 24 hours
- **Password reset links**: Valid for 1 hour

Users can request new links if their tokens expire.

## Migration Behavior

When enabling email verification on an existing instance:

- **Existing users are automatically marked as verified** (grandfathered)
- Only new registrations after enabling the feature require verification
- No action needed for current users

## Security Considerations

1. **Use secure SMTP connections**: Always enable TLS or SSL
2. **Use app-specific passwords**: When available (Gmail, etc.)
3. **Set a strong SECRET_KEY**: Token security depends on your Flask secret key
4. **Consider dedicated email services**: SendGrid, Mailgun, and SES offer better deliverability than personal email accounts

## Troubleshooting

### Emails not sending

1. Check Docker logs: `docker compose logs -f app`
2. Verify SMTP credentials are correct
3. Ensure SMTP port is not blocked by firewall
4. Try sending a test email using the same credentials from another tool

### Emails going to spam

1. Use a proper `SMTP_FROM_ADDRESS` that matches your domain
2. Configure SPF and DKIM records for your domain
3. Consider using a dedicated email service with good reputation

### Verification link not working

1. Ensure `SECRET_KEY` hasn't changed since the email was sent
2. Check if the link has expired (24 hours for verification, 1 hour for reset)
3. Verify your Speakr instance is accessible at the URL in the email

### "SMTP not configured" errors

Ensure all required SMTP variables are set:
- `SMTP_HOST`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`

---

Next: [SSO Setup](sso-setup.md) →
