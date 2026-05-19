# Donation & verification system — operator guide

This guide is for people who run **`!` commands** in the **admin command channel**. Access to that channel is how you control who can administer the bot — commands do **not** require the Discord "Manage Server" permission (except `!setvisibility`).

Members use **slash commands** (`/verifywallet`, `/verifystatus`, etc.) in the main server.

---

## How the system fits together

1. **Snapshot** — A one-time list of holder wallets and how much FARTBOY each held at go-live. That holding amount is fixed afterward. The bot stores this in its **snapshot database**.
2. **Tracker** — A background process that watches the war chest on-chain, records incoming transfers in the **transaction database**, and updates donation totals on snapshot rows.
3. **Wallet link** — Ties a wallet address to a Discord member (via OTP dust or `!manualverify`). Stored on the snapshot row for that wallet.
4. **Transaction attribution** — Ties a specific on-chain transaction to a member (`!addtransaction … @member`), including sends from **exchange** hot wallets. This updates totals without pretending the exchange wallet is the member's personal wallet.
5. **Perks** — **Base** eligibility (1% rule or admin override), then higher **tiers** from total donated USD.

### Donation totals (two stores)

| Field | Table | Used for |
|-------|--------|----------|
| Per-wallet USD | `fartboy_holders.donated_usd` | **My wallets**, 1% FARTBOY progress |
| Member total USD | `verified_users.total_donated_usd` | **Public leaderboard**, **donor tiers**, API |

The bot rebuilds `verified_users` from snapshot rows plus attributed transactions. If these diverge (e.g. leaderboard shows **$0** but My wallets shows the correct amount), run **`!recomputesummaries`** in the admin channel, then **`!syncdonorroles`** if tier roles need updating.

---

## Golden rules

1. **Linking ≠ attributing** — OTP and `!manualverify` **link** a wallet to a member. `!addtransaction … @member` **attributes** a transaction (needed for exchanges).
2. **Base perks vs USD totals** — Attributing exchange donations can make **USD totals and tier roles** correct, but **base** perks still need either the automatic **1% on a linked snapshot wallet with a positive holding**, or `!setuserthreshold @member true`.
3. **Never steal a wallet link** — If a wallet is already linked to someone else, `!manualverify` **refuses**. Use `!removeverification` first, then link to the correct member.
4. **`!resetverification` is nuclear** — Wipes all links and OTP state in the snapshot database. Not for fixing one person.

---

## Verification policy (strict)

Automatic OTP verification works like this:

- The member's wallet must be on the **snapshot** with a **positive** holding amount.
- They must have donated **at least 1%** of that holding to the war chest **before** sending the OTP dust amount.
- If they send the OTP too early, verification is **rejected**, the OTP stays valid, and they can donate more and try again. `/verifystatus` can show recent rejections.

Wallets **not** on the snapshot (including **exchange** hot wallets) cannot complete automatic OTP. Use manual steps below.

The command `!setverificationorder` can switch to a looser "flexible" mode if you ever need it; **strict is the default** and what this guide assumes.

---

## Quick decision: which command?

| Situation | What to do |
|-----------|------------|
| Holder on snapshot; normal self-serve flow | Member: donate toward 1% → `/verifywallet` → send OTP dust → `/verifystatus` |
| Tracker missed a donation | `!addtransaction SIGNATURE` or `… @member` |
| Donation from **exchange** | `!setexchangewallets` (if needed) → `!addtransaction SIGNATURE @member` → often `!setuserthreshold @member true` for base perks |
| Personal wallet **not** on snapshot | `!manualverify @member WALLET` → often `!setuserthreshold @member true` |
| Wallet linked to **wrong** member | `!removeverification WALLET` → `!manualverify @correctMember WALLET` |
| Override base perks for a member | `!setuserthreshold @member true|false|reset` only |
| Force someone on/off the public leaderboard | `!setvisibility @member visible|hidden` |
| Inspect data | `!tx @member`, `!checkwallet WALLET`, `!donorstats`, `!donortop` |
| Pre-launch full reset | `!resetverification` (dangerous) + new snapshot |

---

## Go-live flow (first-time setup)

Run these in the admin channel, in order:

```text
1. !snapshotholders                          ← pulls current holder balances from chain
2. !setdonationleaderboard #donations-channel ← posts the leaderboard embed in that channel
3. !golive                                    ← sets the on-chain checkpoint so old history is ignored
```

**Optional — donor tiers (after go-live):**

```text
4. !setbaserole Contributor                   ← base role for anyone who qualifies
5. !settier 50 🥉 Bronze Donor               ← repeat for each tier
6. !settier 100 🥈 Silver Donor
7. !settier 500 👑 Gold Donor
8. !syncdonorroles                            ← force the first role sync
```

**Optional — fundraising targets:**

```text
9. !settarget 25000 Phase 2                   ← shows progress toward $25k goal
```

**Optional — admin logging:**

```text
10. !setlogchannel #admin-logs                ← OTP assignments are logged here
```

Tracking stays disabled until `!golive` is run. After go-live the tracker polls automatically at the configured interval.

---

## Member slash commands

These are what your server members see. They run in any channel where the bot is present.

### `/verifywallet`

Gives a unique **OTP** amount of FARTBOY and the war chest address.

**Order for members on the snapshot:**

1. Donate until they have met **1%** of their snapshot holding from **that wallet**.
2. Run `/verifywallet` and send the **exact** OTP amount from **that wallet**.
3. Check `/verifystatus` (the bot does not DM them when verification succeeds).

**Example:**

```text
Alice held 10,000 FARTBOY at snapshot → she must donate ≥ 100 FARTBOY from her wallet first.
Then she runs /verifywallet, sends e.g. 0.00123 FARTBOY, and checks /verifystatus.
```

**Do not** tell exchange-only donors to use `/verifywallet` from a CEX address — it will not link. Use admin attribution instead.

### `/verifystatus`

Triggers an on-demand tracker pass (when configured) then shows a single clear status message:

- **Verified** — wallet is linked; shows linked wallet address(es).
- **Pending** — active OTP, no rejection; shows OTP amount and war chest address.
- **Attempt received, not completed** — active or expired OTP with a strict-mode rejection; explains the reason in plain language, shows FARTBOY 1% progress (donated vs needed), and advises donating more before retrying.
- **No verification started** — no OTP history at all.

A compact verification history follows with friendly labels (`awaiting send`, `verified`, `expired`).

### `/mywallets`

Shows the member's verified wallets and the donated USD for each one.

### `/mytransactions`

Shows the member's recent incoming transactions to the war chest. Useful for them to confirm their donation was picked up.

### `/leaderboard`

Shows the full donation leaderboard with ephemeral pagination. Only the requesting user sees it.

### `/leaderboardvisibility`

Lets a member toggle whether their name shows on the public leaderboard. Hiding does **not** remove perks or roles — it only makes them anonymous on the leaderboard.

---

## Admin `!` commands — full reference

Run these only in the **admin command channel**. Anyone who can post there is treated as authorized (except `!setvisibility`, which also requires **Manage Server**).

Tip: run **`!help`** in the admin channel for a quick command list, or **`!help <command>`** for details on a specific command.

---

### Verification & wallet management

#### `!manualverify @Member WALLET_ADDRESS`

**Links** a wallet to a member on the snapshot.

| Case | Behavior |
|------|----------|
| Wallet on snapshot, unlinked | Sets the link |
| Wallet on snapshot, already linked to **this** member | Updates name / visibility as today |
| Wallet on snapshot, linked to **another** member | **Refuses** — shows who has it; use `!removeverification` first |
| Wallet **not** on snapshot | Inserts a row with **zero** holding; reminds you that automatic 1% will never apply — use `!setuserthreshold` if they should get base perks |
| Wallet is an **exchange** address | **Refuses** — use `!addtransaction` instead |

**Example — wrong person had the wallet:**

```text
!manualverify @Alice 7xKX…
→ Bot: already linked to @Bob. Run !removeverification 7xKX… first.

!removeverification 7xKX…
!manualverify @Alice 7xKX…
```

**Example — wallet never in snapshot CSV:**

```text
!manualverify @Alice 9abc…def
→ Linked. Bot hints: run !setuserthreshold @Alice true for base perks if needed.

!setuserthreshold @Alice true
```

---

#### `!removeverification WALLET_ADDRESS`

**Unlinks** one wallet: clears the member link on that snapshot row and clears attribution on transaction rows sent **from** that wallet. Rebuilds affected summaries.

**Use when:** Wrong link, or reassigning wallet to another member.

**Do not use when:** You only meant to reset one member's threshold — use `!setuserthreshold @member reset`.

---

#### `!setuserthreshold @Member true|false|reset`

The **only** command that sets admin **base perk** overrides for a member.

| Value | Meaning |
|-------|---------|
| `true` | Member always passes **base** eligibility (trust admin) |
| `false` | Member never passes **base**, even if 1% would pass |
| `reset` | Remove override; only automatic 1% (and linked wallets) apply |

**When to use `true`:** Exchange-only donors, or wallets manually added with zero snapshot holding.

**When not to use:** To attribute a transaction — use `!addtransaction`. To move a wallet — use `!removeverification` + `!manualverify`.

**Example — exchange donor:**

```text
!addtransaction 5VER…fullSignature… @Alice
!setuserthreshold @Alice true
```

---

#### `!setvisibility @Member visible|hidden`

Admin command to show or hide a user on the public leaderboard. Requires **Manage Server** permission.

```text
!setvisibility @Alice hidden
→ Alice is now anonymous on the leaderboard (perks unchanged).

!setvisibility @Alice visible
→ Alice's name shows again.
```

The user must have at least one verified wallet. This is separate from the member's own `/leaderboardvisibility` preference.

---

#### `!setverificationorder strict|flexible`

Prints or changes OTP policy. **Default is strict.** Only use `flexible` if you intentionally want OTP to link **before** 1% is met.

Run with no argument to see the current setting:

```text
!setverificationorder
→ Current verification order: strict
```

---

#### `!resetverification`

**Snapshot database only:**

- Clears **all** wallet → member links and leaderboard flags on snapshot rows.
- Deletes all **verified member summaries** and **OTP** records.

**Does not:**

- Clear transaction attribution in the transaction database.
- Clear `!setuserthreshold` overrides (run `reset` per member if needed).

**Use only** before go-live or a planned rebuild.

---

### Exchange wallets

#### `!setexchangewallets EXCHANGE_NAME WALLET1 WALLET2 …`

Marks addresses as **exchange** hot wallets. They are skipped for OTP and cannot be `!manualverify`'d.

```text
!setexchangewallets binance HotWallet1 HotWallet2
```

#### `!removeexchangewallets WALLET1 …`

Removes exchange marking. Does not restore old links.

```text
!removeexchangewallets HotWallet1
```

---

### Transactions

#### `!addtransaction SIGNATURE [@Member]`

Fetches a transaction from the chain, records it in the **transaction database**, updates donation totals on snapshot rows by sender wallet, and optionally attributes all legs of that signature to a member.

| Usage | Effect |
|-------|--------|
| `!addtransaction SIG` | Record only |
| `!addtransaction SIG @Alice` | Record + attribute to Alice (works for **exchange** senders) |

**Does not** link an exchange hot wallet to Alice on the snapshot — only attributes the donation.

**Example:**

```text
!addtransaction 3xYz… @Alice
```

If Alice needs **base** role but only donates via exchange: add `!setuserthreshold @Alice true`.

After **`!manualverify`**, prefer **`!addtransaction SIG @Alice`** (not `SIG` alone) so the member's `verified_users` total is rebuilt immediately. If you already recorded the tx without `@user`, run **`!recomputesummaries`** once.

#### `!tx @Member` or `!tx WALLET_ADDRESS`

Look up recent transactions for a user or wallet. Shows up to the configured lookup limit. Use this before changing links or thresholds.

```text
!tx @Alice
!tx 7xKXtg2…
```

#### `!checkwallet WALLET_ADDRESS`

Check a wallet against the snapshot balances. Returns the snapshot holding for the wallet if found.

```text
!checkwallet 7xKXtg2…
```

---

### Leaderboard & display

#### `!setdonationleaderboard #channel [limit]`

Posts the donation leaderboard and recent transaction embeds in the given channel. Also posts the war chest address and interactive buttons. The leaderboard auto-updates on each tracker cycle.

```text
!setdonationleaderboard #donations
!setdonationleaderboard #donations 50
```

#### `!setdonationleadersize [limit]`

Updates how many entries the donation leaderboard shows. Limit is clamped between 1 and 100.

```text
!setdonationleadersize 50
```

---

### Fundraising targets

#### `!settarget <amount_usd> [name]`

Adds a fundraising goal. Amount is in USD. Name is optional.

```text
!settarget 25000 Phase 2
!settarget 5000
```

#### `!removetarget <id>`

Removes a target by its ID number (use `!targets` to see IDs).

```text
!removetarget 3
```

#### `!targets`

Shows all active targets with progress bars and the current total raised.

---

### Donor tiers & roles

#### `!setbaserole <role_name>`

Sets the base contributor role given to anyone who qualifies. The role is created automatically if it doesn't exist on the server.

```text
!setbaserole Contributor
```

Members still need **base** eligibility first (1% on a linked wallet with positive snapshot holding, or `!setuserthreshold true`).

#### `!settier <min_usd> <emoji> <role_name>`

Adds a donation tier. Donors at or above the USD threshold get the emoji as a nickname prefix and the role assigned. Only the **highest** qualifying tier's emoji and role are applied.

```text
!settier 50 🥉 Bronze Donor
!settier 100 🥈 Silver Donor
!settier 500 👑 Gold Donor
```

#### `!removetier <id>`

Removes a tier by its ID number (use `!tiers` to see IDs).

#### `!tiers`

Shows all active donation tiers and the base role.

#### `!recomputesummaries`

Rebuild all `verified_users.total_donated_usd` from snapshot + transaction data. Refreshes leaderboard embeds afterward.

**When to use:** Leaderboard or tier amounts seem wrong; after manual DB changes; after attributing transactions without `@user`.

#### `!syncdonorroles`

Recomputes summaries **and** then force-syncs every verified user's Discord roles and nickname emoji prefix against the configured tiers. Normally runs automatically after each tracker cycle, but this triggers it manually.

**When to use:** After changing tiers, after `!recomputesummaries`, or if roles look out of sync.

---

### Analytics

#### `!donorstats [min_usd]`

Shows overall donation statistics: verified members, wallet counts, total donated, and members meeting the 1% threshold. Optional `min_usd` filters to count donors above that amount.

```text
!donorstats
!donorstats 100
```

#### `!walletsummary`

Shows a breakdown of verified vs unverified wallets and aggregate holdings (tokens held, tokens donated).

#### `!donortop [count]`

Shows top donors ranked by USD donated. Defaults to top 10, maximum 25.

```text
!donortop
!donortop 20
```

---

### Setup & tuning

#### `!snapshotholders`

Run the holder snapshot script — pulls current on-chain balances into the snapshot database. Use before `!golive` or after a planned reset.

#### `!golive`

Sets a fresh on-chain checkpoint so old transactions are ignored. Enables tracking. Run once after `!snapshotholders`.

#### `!settrackerinterval SECONDS`

Changes how often the tracker polls the chain for new transactions. Lower = more API calls. Clamped between 5 and 3600 seconds.

```text
!settrackerinterval 60
```

#### `!setpagelimit LIMIT`

Changes how many signatures per address the tracker fetches per page from Helius. Higher = catches more transactions per poll. Clamped between 1 and 1000.

```text
!setpagelimit 25
```

#### `!setlogchannel #channel`

Sets the admin channel where OTP assignments are logged (username + OTP amount). No argument prints the current log channel.

```text
!setlogchannel #admin-logs
!setlogchannel
→ OTP log channel: #admin-logs
```

---

### Diagnostics

#### `!debugroles`

Dumps all guild roles (name → ID) and the bot's stored base role / tier role IDs. Useful when roles look misconfigured.

#### `!synccommands`

Forces a sync of slash commands with Discord. Run after slash commands are added or changed.

#### `!listcommands`

Lists all registered global and guild slash commands by name.

#### `!help [command]`

Shows all commands grouped by category. Pass a command name for detailed usage.

```text
!help
!help addtransaction
```

---

## Base perk eligibility (recap)

1. Admin set **`false`** → never base-eligible.
2. Admin set **`true`** → always base-eligible.
3. Otherwise → **any** linked wallet on the snapshot with **positive** holding and donations ≥ **1%** of that holding.

Exchange attribution alone does **not** satisfy (3). Use `!setuserthreshold @member true` when policy allows.

---

## Scenario cookbook

### A — Snapshot holder (normal self-serve)

1. Alice donates until ≥ 1% of her snapshot holding from wallet W.
2. Alice runs `/verifywallet`, sends OTP dust from W.
3. Tracker links W to Alice. She earns base perks automatically when 1% is met (may already be met before OTP).

### B — Two wallets, same member

1. Alice completes strict OTP for wallet W1, then for W2.
2. Base perks if **either** wallet meets 1% on the snapshot.
3. Both wallets' donated USD roll up into her single leaderboard total.

### C — Reassign a wallet to a different member

```text
!removeverification 7xKX…
!manualverify @Alice 7xKX…
```

### D — Exchange deposit (most common admin task)

Alice sends FARTBOY from Coinbase to the war chest and DMs an admin with the transaction signature.

```text
1. !setexchangewallets coinbase HotWallet…      ← only needed the first time for that exchange
2. !addtransaction <sig> @Alice                  ← attributes the donation to Alice
3. !setuserthreshold @Alice true                 ← grants base perk eligibility
```

Her USD total and tier role update immediately. If she later verifies a personal wallet via OTP, she keeps credit for both.

### E — Off-snapshot personal wallet

A new holder who was not in the original snapshot asks to be added.

```text
1. !manualverify @Alice 9abc…def                 ← creates a snapshot row with balance 0
2. !setuserthreshold @Alice true                 ← grants base perks since 1% of 0 is impossible
```

If they have a specific donation tx to record:

```text
3. !addtransaction <sig> @Alice                  ← records the donation amount for leaderboard
```

### F — OTP too early (member didn't donate enough before sending OTP)

**Symptom:** `/verifystatus` shows a rejection; OTP still active.

**Fix:** Tell the member to donate more until 1% is met, then send the **same** OTP amount again (it stays valid). If the OTP expired, they run `/verifywallet` again to get a new one.

### G — Leaderboard shows $0 for a verified member

This means `verified_users.total_donated_usd` is out of sync with the snapshot or transaction data.

```text
!recomputesummaries
```

If tier roles also need fixing:

```text
!syncdonorroles
```

### H — Adding a new fundraising target mid-campaign

```text
!settarget 50000 Marketing Push
→ Target #3 added: $50,000.00 (Marketing Push)

!targets
→ Shows all targets with current progress
```

### I — Changing tier structure

```text
!tiers                            ← see current tiers and their IDs
!removetier 2                     ← remove the old tier
!settier 200 🥈 Silver Donor     ← add the new threshold
!syncdonorroles                   ← apply to all members immediately
```

### J — Member claims they donated but nothing shows up

1. Ask for their wallet address or tx signature.
2. Check the data:

```text
!tx @Member                       ← shows attributed transactions
!checkwallet <their_wallet>       ← shows snapshot balance and donations
```

3. If the tx is missing:

```text
!addtransaction <sig> @Member     ← manually record it
```

4. If it was from an exchange:

```text
!setexchangewallets <name> <hot_wallet>    ← if not already marked
!addtransaction <sig> @Member
!setuserthreshold @Member true             ← if they need base perks
```

---

## OTP outcomes (strict)

| Situation | OTP | Result |
|-----------|-----|--------|
| 1% met, dust matches OTP | Used | Wallet linked to member |
| 1% not met, dust matches OTP | Stays assigned | Rejection logged; member can retry |
| Dust does not match anyone's OTP | — | No link |
| OTP expired | Expired | Member runs `/verifywallet` again |
| Exchange sender | Skipped | Use `!addtransaction @member` |
| Wallet already another member's | — | OTP cannot steal link; admin uses `!removeverification` |

---

## REST API (optional)

If `API_KEY` is set in the `.env` file, an HTTP API starts alongside the bot on the configured `API_PORT` (default 8000). This is for external websites or dashboards to read donation data.

- All requests require the header `x-api-key` matching the configured `API_KEY`.
- The API is **read-only** and does not modify any data.
- Endpoints include stats, leaderboard, and recent transactions.
- CORS origins are configured via `API_CORS_ORIGINS` in `.env`.

---

## Environment variables (reference)

| Variable | Purpose | Default |
|----------|---------|---------|
| `DISCORD_BOT_TOKEN` | Bot login token | (required) |
| `DISCORD_GUILD_ID` | Server ID for slash commands | (required) |
| `HELIUS_API_KEY` | Helius RPC key for chain data | (required) |
| `FARTBOY_MINT` | Token mint address | (required) |
| `WARCHEST_ADDRESS` | War chest wallet to track | (required) |
| `COMMAND_CHANNEL_ID` | Restrict `!` commands to this channel | (empty = allow all) |
| `SNAPSHOT_DB` | Path to snapshot database | `/app/data/fartboy_snapshot.db` |
| `TX_DB` | Path to transaction database | `/app/data/incoming_transactions.db` |
| `LEADERBOARD_STATE_DB` | Path to leaderboard state DB | `/app/data/leaderboard_state.db` |
| `LEADERBOARD_UPDATE_SECONDS` | Leaderboard auto-refresh interval | `300` |
| `TRACKER_INTERVAL_SECONDS` | Chain polling interval | `30` |
| `OTP_EXPIRY_SECONDS` | How long an OTP stays valid | `3600` (1 hour) |
| `TX_LOOKUP_LIMIT` | Max transactions shown in `!tx` | `50` |
| `API_KEY` | Enable REST API (leave empty to disable) | (empty) |
| `API_PORT` | REST API port | `8000` |
| `API_HOST` | REST API bind address | `0.0.0.0` |
| `API_CORS_ORIGINS` | CORS allowed origins | `*` |

---

## Troubleshooting

| Problem | Likely cause | Fix |
|---------|-------------|-----|
| Bot ignores `!` commands | Wrong channel or bot offline | Check `COMMAND_CHANNEL_ID` in `.env`; confirm bot is running |
| Slash commands not showing | Commands not synced | `!synccommands` |
| Leaderboard stuck / not updating | Tracker not live or interval too high | `!golive` if not live; `!settrackerinterval 60` |
| Member shows $0 on leaderboard | Summary out of sync | `!recomputesummaries` then `!syncdonorroles` |
| Tier roles not applied | Tiers not configured or sync not run | `!tiers` to check, `!syncdonorroles` to force |
| "Tracker is not configured" | Missing `HELIUS_API_KEY`, `WARCHEST_ADDRESS`, or `FARTBOY_MINT` | Set them in `.env` and restart |
| OTP expired before member sent | Default expiry is 1 hour | Member runs `/verifywallet` again for a fresh OTP |
| `!manualverify` says "already linked" | Wallet belongs to someone else | `!removeverification WALLET` first |
| Bot can't assign roles | Bot role is below the target role in Discord hierarchy | Move the bot's role higher in Server Settings → Roles |

---

## Notes

- Verification outcomes are shown via **ephemeral** `/verifystatus` in the server (no DMs).
- `!` commands do **not** require **Manage Server** on Discord — access to the admin command channel is enough (except `!setvisibility`).
- The tracker accepts FARTBOY, USDC, USDT, and SOL donations. Non-FARTBOY donations under $0.01 are ignored.
- Prices are fetched from Jupiter and CoinGecko for USD conversion; if pricing fails, FARTBOY donations are still recorded with a $0 value and can be recalculated later with `!recomputesummaries`.
