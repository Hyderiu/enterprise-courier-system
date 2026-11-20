# Security Policy

## Supported use

This public edition is intended for local evaluation and code review. It is not
preconfigured as an internet-facing production service.

## Reporting a vulnerability

Please report vulnerabilities privately to the repository owner. Do not place
credentials, personal data, exploit payloads, or production endpoints in a
public issue.

## Secrets

Never commit `.env`, databases, logs, exports, billing files, private keys,
carrier credentials, DingTalk credentials, CRM credentials, shop IDs or monthly
billing account identifiers. If a secret is exposed, revoke and rotate it first;
removing it from the latest commit is not sufficient.
