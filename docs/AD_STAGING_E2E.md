# Advertising staging end-to-end check

## Purpose

`relocant-ad-staging-check` verifies the advertising workflow end to end without using a real
Telegram account, real Telegram Ads credentials, a production database, or money-changing APIs.

The check intentionally runs against an isolated in-memory SQLite database and synthetic Ads
measurements. It exercises the same application services used by the advertising workflow:

1. `/ad` private intake creates an `AdRequest`.
2. Suggested-post review records an `accept_candidate` decision for a safe synthetic local ad.
3. Pricing evaluates the candidate against `cities/belgrade/ad-pricing.yaml`.
4. Read-only Ads analytics evaluates a synthetic campaign snapshot.
5. Safety assertions verify that no stage has publication, commercial-send, or spend capability.

## Current expected result

The committed Belgrade pricing policy is intentionally unconfigured:

- `configured: false`
- `currency: null`
- no package IDs
- no monetary defaults
- `approval.mode: owner_always`

Therefore the staging check is expected to return:

```text
pricing_status = owner_configuration_required
pricing_policy_configured = false
```

This is a successful fail-closed result. The check must not invent a fallback currency, package or
price in order to make the flow appear complete.

## Run

```bash
relocant-ad-staging-check
```

The command exits with code `0` only when all safety assertions pass. Its JSON result explicitly
reports:

- `network_calls: 0`
- `telegram_mutations: 0`
- `ads_money_mutations: 0`
- whether advertiser intake succeeded;
- whether review remained candidate-only and owner-gated;
- whether pricing exposed no send/publication/spend capability;
- whether unconfigured pricing failed closed;
- whether Ads analytics remained read-only.

## What this proves

The dry-run verifies the application-level wiring from advertiser intake through review, pricing
and analytics. It also provides a regression test that a future refactor does not accidentally
turn this staging path into a delivery or money-changing path.

It does **not** prove:

- that a real advertiser can complete the flow through Telegram UX;
- that a commercial offer was sent;
- that a sponsored post was published;
- that Telegram Ads credentials work;
- that any real campaign was created or modified;
- that the channel has a validated market price.

## Remaining owner input

Before a real commercial price can be proposed, the owner must explicitly define the rate card in
`cities/belgrade/ad-pricing.yaml`: currency, package IDs, base prices and permitted bounds. Those
values are business decisions and are intentionally not inferred from synthetic data.

Even after a rate card is configured, publication and any future Telegram Ads bid/budget/spend
mutation remain separate capabilities and must keep their existing approval boundaries.
