# Solana Crowdfunding Platform 🚀

A decentralized crowdfunding smart contract on Solana — think Kickstarter, but on-chain.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Solana](https://img.shields.io/badge/Solana-1.18-blue)](https://solana.com)

## 🎯 What It Does

- **Creators** launch campaigns with a goal and deadline
- **Donors** contribute SOL to campaigns they believe in
- **Smart escrow** holds funds until deadline
- **Auto-refunds** if goal isn't reached
- **Secure withdrawals** when campaign succeeds

## 📋 Features

✅ **4 Core Instructions:**
- `CreateCampaign` — Set up a new fundraising campaign
- `Contribute` — Donate SOL to a campaign
- `Withdraw` — Creator claims funds (goal reached)
- `Refund` — Donors get money back (goal not reached)

✅ **Security:**
- PDA vault (no private key = no theft)
- Deadline validation
- Double-withdrawal protection
- Goal-based refund logic

## 🏗️ Architecture

### Campaign Data Structure
```rust
pub struct Campaign {
    pub creator: Pubkey,  // Who created this
    pub goal: u64,        // Target in lamports
    pub raised: u64,      // Current amount
    pub deadline: i64,    // Unix timestamp
    pub claimed: bool,    // Already withdrawn?
    pub bump: u8,         // PDA bump seed
}
```

### Program Flow

```
┌─────────────┐
│   Creator   │──────> CreateCampaign(goal, deadline)
└─────────────┘              │
                             ▼
                    ┌──────────────┐
                    │   Campaign   │
                    │   (on-chain) │
                    └──────────────┘
                             │
       ┌─────────────────────┼─────────────────────┐
       ▼                     ▼                     ▼
   Donor A            Donor B                 Donor C
   Contribute(600)    Contribute(300)         Contribute(200)
       │                     │                     │
       └─────────────────────┴─────────────────────┘
                             │
                             ▼
                    ┌──────────────┐
                    │  PDA Vault   │
                    │ (holds funds)│
                    └──────────────┘
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
        Goal Reached?                  Goal Failed?
              │                             │
              ▼                             ▼
        Withdraw()                      Refund()
       (to creator)                  (to donors)
```

---

## 🚀 Quick Start

### Prerequisites
- Rust 1.70+
- Solana CLI 1.18+
- Node.js (for TypeScript client)
- Python 3.8+ (for Python client)

### Installation

```bash
# Clone the repo
git clone https://github.com/CalvinSkunnies/Solana-Crowdfunding.git
cd Solana-Crowdfunding

# Install Solana CLI (if not installed)
sh -c "$(curl -sSfL https://release.anza.xyz/stable/install)"

# Set up devnet
solana config set --url devnet
solana-keygen new
```

### Build the Program

```bash
# Install Solana build tools
cargo install cargo-build-sbf

# Build
cargo build-sbf
```

The compiled program will be at:
```
target/deploy/solana_crowdfunding.so
```

---

## 🛠️ Deployment

### Deploy to Devnet

```bash
# Get some SOL
solana airdrop 2

# Deploy
solana program deploy target/deploy/solana_crowdfunding.so
```

You'll get back a **Program ID** — save it!

### Update Program ID

After deployment, update the program ID in:
- `src/lib.rs` line 47
- `test_client.py` line 17
- `test-client.ts` line 20

Then rebuild and redeploy.

---

## 🧪 Testing

### Python Client

```bash
# Install dependencies
pip install solders solana

# Run tests
python test_client.py
```

### TypeScript Client

```bash
# Install dependencies
npm install @solana/web3.js

# Run tests
npx ts-node test-client.ts
```

### Manual Testing with Solana CLI

```bash
# Create a campaign (example)
solana program call <PROGRAM_ID> \
  --instruction 0 \
  --data "goal:1000000000000,deadline:1711584000"

# Check campaign account
solana account <CAMPAIGN_ADDRESS>
```

---

## 📝 Instruction Reference

### 1. CreateCampaign

**Instruction ID:** `0`

**Data:**
- `goal` (u64): Target amount in lamports
- `deadline` (i64): Unix timestamp

**Accounts:**
1. Creator (signer, writable)
2. Campaign account (writable)
3. System program
4. Rent sysvar

**Validation:**
- Deadline must be in the future

---

### 2. Contribute

**Instruction ID:** `1`

**Data:**
- `amount` (u64): Donation in lamports

**Accounts:**
1. Donor (signer, writable)
2. Campaign account (writable)
3. System program

**Validation:**
- Campaign must not have ended

---

### 3. Withdraw

**Instruction ID:** `2`

**Data:** None

**Accounts:**
1. Creator (signer, writable)
2. Campaign account (writable)

**Validation:**
- Caller must be campaign creator
- Deadline must have passed
- `raised >= goal`
- Not already claimed

---

### 4. Refund

**Instruction ID:** `3`

**Data:**
- `amount` (u64): Amount to refund

**Accounts:**
1. Donor (signer, writable)
2. Campaign account (writable)

**Validation:**
- Deadline must have passed
- `raised < goal`

---

## ❌ Error Codes

| Code | Error | Description |
|------|-------|-------------|
| 0 | DeadlineInPast | Campaign deadline is not in the future |
| 1 | CampaignEnded | Campaign has already ended |
| 2 | GoalNotReached | Cannot withdraw - goal not met |
| 3 | GoalReached | Cannot refund - goal was met |
| 4 | AlreadyClaimed | Funds already withdrawn |
| 5 | NotCreator | Only creator can withdraw |
| 6 | CampaignActive | Campaign still running |
| 7 | InvalidAccount | Invalid account provided |
| 8 | InsufficientFunds | Not enough funds in vault |

---

## 🧪 Test Scenarios

### Scenario 1: Successful Campaign
```bash
1. Create campaign: goal=1000 SOL, deadline=tomorrow
2. Contribute 600 SOL → raised=600 ✅
3. Contribute 500 SOL → raised=1100 ✅
4. Try withdraw before deadline → ❌ CampaignActive
5. Wait until after deadline
6. Withdraw → ✅ 1100 SOL to creator
7. Try withdraw again → ❌ AlreadyClaimed
```

### Scenario 2: Failed Campaign
```bash
1. Create campaign: goal=1000 SOL, deadline=tomorrow
2. Contribute 400 SOL → raised=400 ✅
3. Contribute 200 SOL → raised=600 ✅
4. Wait until after deadline
5. Try withdraw → ❌ GoalNotReached
6. Refund 400 SOL → ✅ (donor 1)
7. Refund 200 SOL → ✅ (donor 2)
```

---

## 🔐 Security Considerations

### PDA Vault
Funds are stored in a Program Derived Address (PDA), not controlled by any private key. Only the program can sign for transfers from it.

```rust
let (vault_pda, bump) = Pubkey::find_program_address(
    &[b"vault", campaign_account.key.as_ref()],
    program_id,
);
```

### Reentrancy Protection
- Campaign state is updated before transfers
- `claimed` flag prevents double withdrawals

### Validation Checks
- All time-based logic uses on-chain clock (no manipulation)
- Creator verification on withdrawals
- Goal checks on both withdraw and refund

---

## 📊 Gas Costs (Devnet Estimates)

| Operation | Cost (SOL) |
|-----------|-----------|
| Create Campaign | ~0.002 |
| Contribute | ~0.000005 |
| Withdraw | ~0.000005 |
| Refund | ~0.000005 |

*Note: Mainnet costs may vary*

---

## 🛣️ Roadmap

- [ ] Multi-currency support (SPL tokens)
- [ ] Milestone-based releases
- [ ] Campaign categories/tags
- [ ] Frontend UI (React)
- [ ] Campaign updates/announcements
- [ ] NFT rewards for backers

---

## 🤝 Contributing

Contributions welcome! Please:
1. Fork the repo
2. Create a feature branch
3. Submit a PR with tests

---

## 📄 License

MIT License - see [LICENSE](LICENSE)

---

## 🙏 Acknowledgments

- Built with [Solana](https://solana.com)
- Inspired by Kickstarter & GoFundMe
- Uses [Borsh](https://borsh.io) for serialization

---

## 📞 Support

- **Issues:** [GitHub Issues](https://github.com/CalvinSkunnies/Solana-Crowdfunding/issues)
- **Telegram:** @CalvinSkunnies
- **Docs:** [Solana Docs](https://docs.solana.com)

---

## 📚 Resources

- [Solana Program Library](https://spl.solana.com/)
- [Solana Cookbook](https://solanacookbook.com/)
- [Anchor Framework](https://www.anchor-lang.com/) (future migration?)

---

**Built with ❤️ on Solana**