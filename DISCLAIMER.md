# Disclaimer & Risk Notice

**Read this before using Krypt Trader with real money.**

Krypt Trader is free, open-source, experimental software for placing trades on
[Kalshi](https://kalshi.com). By downloading, building, or running it, you
acknowledge and accept everything below.

## Not financial advice
Krypt Trader, its strategies, signals, scores, and any documentation are for
informational and educational purposes only. Nothing here is financial,
investment, legal, or tax advice. The authors are **not** registered investment
advisors, commodity trading advisors, broker-dealers, or fiduciaries of any
kind, and nothing in this project creates such a relationship.

## Risk of loss
Trading event contracts involves substantial risk. **You can lose some or all
of the money in your account.** Automated trading can lose money quickly and at
scale, including while you are away from your computer. Only trade with money
you can afford to lose entirely.

## The strategies are unproven
The bundled strategies (whale tracker, momentum scanner, 15-minute crypto, etc.)
are **heuristics**. They:

- have **not** been validated with out-of-sample backtesting;
- do **not** currently account for Kalshi trading fees in their entry/sizing
  decisions, which can erode or eliminate any apparent edge;
- carry **no guarantee of profitability**.

Any performance figures, "edge" scores, or calibration claims are illustrative
and are **not** a promise of future results. Past or simulated performance does
not indicate future performance.

## No warranty
The software is provided "AS IS", without warranty of any kind, as stated in the
[LICENSE](./LICENSE). It may contain bugs that cause incorrect orders, missed
orders, or inaccurate P&L. The authors are not liable for any losses, damages,
or claims arising from its use.

## Your responsibilities
You are solely responsible for:

- Complying with [Kalshi's Terms of Service](https://kalshi.com/terms) and API
  terms, including any rules on automated/algorithmic trading. **Confirm that
  automated trading with your account is permitted before enabling it.**
- Complying with all laws and regulations in your jurisdiction, including
  eligibility, age, and licensing requirements.
- The security of your own Kalshi API keys and the machine you run this on.
- Every order the software places on your behalf.

## Use demo + dry-run first
Krypt Trader ships defaulted to Kalshi's **demo** environment with **dry-run**
enabled. Keep it that way until you fully understand the software and the risks.
Going live requires deliberately disabling both safeguards.

## Anonymous usage data
To power the community leaderboard and to improve the tool, Krypt Trader sends
**anonymous** usage data — your profit/loss statistics and your strategy
*settings*. It **never** sends your API keys, RSA private key, account
credentials, account number, balance you didn't choose to share, name, email,
IP address, or any per-install or personal identifier. Every report is just
"a user" and cannot be linked across time or back to your account. This happens
only on the **production** environment and only for a **profitable session**.
You can disable it entirely by setting the environment variable
`KRYPT_LEADERBOARD=0`.

## Affiliate disclosure
Links to Kalshi in this app and its documentation are **referral links**. If you
sign up through one, Kalshi may credit both you and the authors. This is a
material connection; using a referral link is optional and costs you nothing
extra.

## No affiliation
Krypt Trader is an independent project and is **not affiliated with, endorsed by,
or sponsored by** Kalshi, Discord, or any data provider. Your use of those
services is governed by their own terms, and you are responsible for complying
with them.

## Limitation of liability
To the maximum extent permitted by law, the authors and contributors shall not
be liable for any direct, indirect, incidental, special, consequential, or
exemplary damages — including, without limitation, trading losses, lost profits,
missed or erroneous orders, data loss, or account actions taken by Kalshi —
arising out of or relating to your use of (or inability to use) this software,
even if advised of the possibility of such damages. Your sole remedy is to stop
using the software.

If you do not agree with any of the above, do not use this software.

## Perpetual Futures (Kalshi "margin")

The Perpetuals features (data recorder, volume farmer, strategy builder,
backtester, paper mode, and live mode) interact with LEVERAGED derivatives.
In addition to everything above:

- **You can lose more than your posted margin.** Leveraged positions are
  liquidated automatically by the exchange when the market moves against you.
  At 5x leverage, roughly a 2% adverse move can wipe a position.
- **Fees are charged on notional, not margin.** A "small" position pays fees
  on its full size every fill. Funding payments accrue every 8 hours while a
  position is held.
- **Backtests and paper trading are simulations.** They use honest fill rules
  but cannot capture live slippage, partial fills, outages, liquidation
  engine behavior, or your own latency. A profitable backtest is more often
  an overfit artifact than a discovery — our own published audit of this
  venue (bundled under `python/data/research/`) backtested eleven strategy
  families and found zero profitable configurations.
- **The strategy builder executes YOUR rules.** The authors provide the tool,
  not the strategy; nothing in this software is investment advice, and no
  outcome is warranted. Use the daily loss caps, start in paper mode, and
  never fund the perps wallet with money you cannot afford to lose.
