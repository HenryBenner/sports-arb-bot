# Kalshi shard preflight and sports mapping

The execution change is limited to submission preflight and explicit Kalshi
routing. Profitability, batch sizing, FOK order mechanics, order sequence, retry
limits, fee formulas, and the cross-50 guard retain their existing behavior.

Before either order, the executor resolves the exact Kalshi ticker's
`exchange_index`, attaches it to the immutable leg, and verifies active market
status, shard cash covering limit-price notional plus fees, and sufficient FOK
depth at the approved limit. Missing, invalid, changed, or unavailable shard
metadata blocks submission. Hedge retries preserve the attached index.

Kalshi balance requests include `exchange_index`. `balance_dollars` is read as
dollars; integer `balance` is read as cents, including balances below $100.
See [Kalshi balance documentation](https://docs.kalshi.com/api-reference/portfolio/get-balance)
and [V2 order routing](https://docs.kalshi.com/api-reference/orders/create-order-v2).

Numeric International tokens select their own outcome; PredictionHunt's generic
group side cannot override it. Exact US `marketSides.long` metadata controls the
US side. The current PredictionHunt leg price is passed in cents for a fresh
sanity check on every resolution. Strong evidence of reversed orientation rejects
the mapping with `orientation_price_suspicious`; prices never flip the side.
Near-50 quotes do not choose orientation. Gamma stored prices are not authority.
Successful mappings log token, sport, both semantic outcomes, US slug/side,
source price, and US quotes when the price check is available.

League aliases cover CFB/NCAA Football, CBB/NCAA Basketball, ITF Men/ITFME,
ITF Women/ITFWO, and ATP Challenger. Participant matching handles accent and
punctuation differences and explicit feed aliases, while preserving uniqueness.
Signed spreads retain the participant and exact signed line; total outcome
labels validate the exact numeric line. Period aliases include first-five-innings
and quarters; conflicting explicit periods reject the mapping.

Scheduled team-sport starts tolerate up to 30 minutes on the same UTC date.
Tennis, cricket, MMA, and boxing starts can differ by up to 18 hours on the same
UTC date only when explicit competition/tournament/card identity agrees.
Participant, league, game-number, round, contract, line, and outcome checks remain
in force. Ambiguous fixtures and insufficiently structured contracts remain
skipped; this does not guarantee coverage of every listed sport or prop type.

GitHub inspection found this checkout seven commits behind remote `main`.
The checkout was fast-forwarded to `dd3b2e7` before these changes. Normal Git
access failed inside the sandbox with a Windows TLS credential error; verified
Git access outside the sandbox succeeded without disabling certificate checks.

These fixes have been validated locally with 308 automated tests and 40
subtests. No live orders were submitted, and deployment has not been verified.
Pushing this commit does not verify deployment. Before enabling
the deployed bot, verify its revision includes these changes and inspect its
mapping/preflight logs. Retain the cross-50 guard while collecting several days
of clean mapping evidence.
