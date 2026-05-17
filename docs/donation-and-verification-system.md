# Donation & verification system — operator guide

This guide is for people who run **`!` commands** in the **admin command channel**. Access to that channel is how you control who can administer the bot — commands do **not** require the Discord “Manage Server” permission.

Members use **slash commands** (`/verifywallet`, `/verifystatus`, etc.) in the main server.

---

## How the system fits together

1. **Snapshot** — A one-time list of holder wallets and how much FARTBOY each held at go-live. That holding amount is fixed afterward. The bot stores this in its **snapshot database**.
2. **Tracker** — A background process that watches the war chest on-chain, records incoming transfers in the **transaction database**, and updates donation totals on snapshot rows.
3. **Wallet link** — Ties a wallet address to a Discord member (via OTP dust or `!manualverify`). Stored on the snapshot row for that wallet.
4. **Transaction attribution** — Ties a specific on-chain transaction to a member (`!addtransaction … @member`), including sends from **exchange** hot wallets. This updates totals without pretending the exchange wallet is the member’s personal wallet.
5. **Perks** — **Base** eligibility (1% rule or admin override), then higher **tiers** from total donated USD.

---

## Golden rules

1. **Linking ≠ attributing** — OTP and `!manualverify` **link** a wallet to a member. `!addtransaction … @member` **attributes** a transaction (needed for exchanges).
2. **Base perks vs USD totals** — Attributing exchange donations can make **USD totals and tier roles** correct, but **base** perks still need either the automatic **1% on a linked snapshot wallet with a positive holding**, or `!setuserthreshold @member true`.
3. **Never steal a wallet link** — If a wallet is already linked to someone else, `!manualverify` **refuses**. Use `!removeverification` first, then link to the correct member.
4. **`!resetverification` is nuclear** — Wipes all links and OTP state in the snapshot database. Not for fixing one person.

---

## Verification policy (strict)

Automatic OTP verification works like this:

- The member’s wallet must be on the **snapshot** with a **positive** holding amount.
- They must have donated **at least 1%** of that holding to the war chest **before** sending the OTP dust amount.
- If they send the OTP too early, verification is **rejected**, the OTP stays valid, and they can donate more and try again. `/verifystatus` can show recent rejections.

Wallets **not** on the snapshot (including **exchange** hot wallets) cannot complete automatic OTP. Use manual steps below.

The command `!setverificationorder` can switch to a looser “flexible” mode if you ever need it; **strict is the default** and what this guide assumes.

---

## Quick decision: which command?

| Situation | What to do |
|-----------|------------|
| Holder on snapshot; normal self-serve flow | Member: donate toward 1% → `/verifywallet` → send OTP dust → `/verifystatus` |
| Tracker missed a donation | `!addtransaction SIGNATURE` or `… @member` |
| Donation from **exchange** | `!setexchangewallets` (if needed) → `!addtransaction SIGNATURE @member` → often `!setuserthreshold @member true` for base perks |
| Personal wallet **not** on snapshot | `!manualverify @member WALLET` → often `!setuserthreshold @member true` |
| Wallet linked to **wrong** member | `!removeverification WALLET` → `!manualverify @correctMember WALLET` |
| Override base perks for a member | `!setuserthreshold @member true\|false\|reset` only |
| Inspect data | `!tx @member`, `!checkwallet WALLET` |
| Pre-launch full reset | `!resetverification` (dangerous) + new snapshot |

---

## Member slash commands

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

Triggers an on-demand tracker pass (when configured), shows OTP history, and any recent **rejection** reasons if they sent OTP too early.

### `/mywallets`, `/mytransactions`, `/leaderboard`, `/leaderboardvisibility`

Self-service views. Leaderboard visibility is optional and does not remove perks.

---

## Admin `!` commands

Run these only in the **admin command channel**. Anyone who can post there is treated as authorized.

### `!manualverify @Member WALLET_ADDRESS`

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

### `!removeverification WALLET_ADDRESS`

**Unlinks** one wallet: clears the member link on that snapshot row and clears attribution on transaction rows sent **from** that wallet. Rebuilds affected summaries.

**Use when:** Wrong link, or reassigning wallet to another member.

**Do not use when:** You only meant to reset one member’s threshold — use `!setuserthreshold @member reset`.

---

### `!setuserthreshold @Member true|false|reset`

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

### `!addtransaction SIGNATURE [@Member]`

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

---

### `!setexchangewallets EXCHANGE_NAME WALLET1 WALLET2 …`

Marks addresses as **exchange** hot wallets. They are skipped for OTP and cannot be `!manualverify`’d.

```text
!setexchangewallets binance HotWallet1 HotWallet2
```

### `!removeexchangewallets WALLET1 …`

Removes exchange marking. Does not restore old links.

---

### `!tx @Member` and `!checkwallet WALLET`

Debugging before you change links or thresholds.

```text
!tx @Alice
!checkwallet 7xKXtg2…
```

---

### `!resetverification`

**Snapshot database only:**

- Clears **all** wallet → member links and leaderboard flags on snapshot rows.
- Deletes all **verified member summaries** and **OTP** records.

**Does not:**

- Clear transaction attribution in the transaction database.
- Clear `!setuserthreshold` overrides (run `reset` per member if needed).

**Use only** before go-live or a planned rebuild.

---

### `!snapshotholders` and `!golive`

**Snapshot:** Refresh holder balances from chain (go-live setup).

**Go live:** Set tracker checkpoint so old history is not ingested.

---

### `!setverificationorder strict|flexible` (optional)

Prints or changes OTP policy. **Default is strict.** Only use `flexible` if you intentionally want OTP to link **before** 1% is met.

---

### Tiers & roles (short)

| Command | Purpose |
|---------|---------|
| `!settier`, `!removetier`, `!tiers` | USD thresholds, emoji, roles |
| `!setbaserole` | Role for anyone who qualifies for any tier |
| `!syncdonorroles` | Force role/nickname sync |

Members still need **base** eligibility first (1% on a linked wallet with positive snapshot holding, or `!setuserthreshold true`).

---

## Base perk eligibility (recap)

1. Admin set **`false`** → never base-eligible.
2. Admin set **`true`** → always base-eligible.
3. Otherwise → **any** linked wallet on the snapshot with **positive** holding and donations ≥ **1%** of that holding.

Exchange attribution alone does **not** satisfy (3). Use `!setuserthreshold @member true` when policy allows.

---

## Scenario cookbook

### A — Snapshot holder (normal)

1. Alice donates until ≥ 1% of her snapshot holding from wallet W.
2. Alice `/verifywallet`, sends OTP dust from W.
3. Tracker links W to Alice. She earns base perks automatically when 1% is met (may already be met before OTP).

### B — Two wallets, same member

1. Alice completes strict OTP for wallet W1, then for W2.
2. Base perks if **either** wallet meets 1% on the snapshot.

### C — Reassign wallet

```text
!removeverification 7xKX…
!manualverify @Alice 7xKX…
```

### D — Exchange deposit

```text
!setexchangewallets coinbase HotWallet…
!addtransaction <sig> @Alice
!setuserthreshold @Alice true
```

### E — Off-snapshot personal wallet

```text
!manualverify @Alice 9abc…def
!setuserthreshold @Alice true
```

### F — OTP too early

Symptom: `/verifystatus` shows a rejection; OTP still active.

Fix: Donate until 1% is met, send the **same** OTP amount again (or get a new OTP after expiry).

---

## OTP outcomes (strict)

| Situation | OTP | Result |
|-----------|-----|--------|
| 1% met, dust matches OTP | Used | Wallet linked to member |
| 1% not met, dust matches OTP | Stays assigned | Rejection logged; member can retry |
| Dust does not match anyone’s OTP | — | No link |
| OTP expired | Expired | Member runs `/verifywallet` again |
| Exchange sender | Skipped | Use `!addtransaction @member` |
| Wallet already another member’s | — | OTP cannot steal link; admin uses `!removeverification` |

---

## What the bot does not do

- It does **not** send “you are verified” DMs. Members use **ephemeral** `/verifystatus`.
- It does **not** require **Manage Server** on Discord for `!` commands — channel access is enough.
