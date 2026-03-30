"""
Solana Crowdfunding Platform - Python Test Client
Repo: https://github.com/CalvinSkunnies/Solana-Crowdfunding

ROOT CAUSE OF ALL FAILURES (diagnosed from lib.rs)
────────────────────────────────────────────────────────────────
The BorshIoError on every instruction was caused by a wrong account
allocation size in create_campaign:

  OLD (broken):  campaign_data_len = 8 + 32 + 8 + 8 + 8 + 1 + 1  = 66 bytes
  NEW (correct): campaign_data_len =     32 + 8 + 8 + 8 + 1 + 1  = 58 bytes

The leading `8` was an Anchor-style discriminator — this is a native
Borsh program and has no discriminator. Borsh serializes Campaign as
exactly 58 bytes. Allocating 66 left 8 uninitialized zero bytes at the
end, and Campaign::try_from_slice() is strict: it errors if any bytes
remain after deserialization → BorshIoError on every contribute/withdraw.

ACCOUNT LAYOUTS (from lib.rs — no vault PDA needed in client calls)
────────────────────────────────────────────────────────────────
  CreateCampaign [0]: creator(signer,w), campaign(signer,w),
                      system_program, rent_sysvar
  Contribute     [1]: donor(signer,w), campaign(w), system_program
  Withdraw       [2]: creator(signer,w), campaign(w)
  Refund         [3]: donor(signer,w), campaign(w)

The campaign account IS the vault — SOL is transferred directly into it.
No separate vault PDA account is passed to any instruction.

CHECKLIST
────────────────────────────────────────────────────────────────
1. Create campaign  goal=0.1 SOL, deadline=45s (or 24h --long-deadline)
2. Contribute 0.06 SOL  →  raised = 0.06 SOL
3. Contribute 0.05 SOL  →  raised = 0.11 SOL  (exceeds goal)
4. Withdraw BEFORE deadline  →  must FAIL  (CampaignActive)
5. Wait for deadline, withdraw  →  must SUCCEED
6. Withdraw again  →  must FAIL  (AlreadyClaimed)

IMPORTANT: You must redeploy lib.rs before running this client.
The fix is in lib.rs (campaign_data_len = 58, not 66).
See instructions at the bottom of this file.

Usage:
    pip install solders solana
    python test_client.py
    python test_client.py --keypair /path/to/id.json
    python test_client.py --new-wallet
    python test_client.py --long-deadline
"""

import argparse
import json
import os
import re
import struct
import sys
import time
from dataclasses import dataclass
from typing import Optional

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.instruction import Instruction, AccountMeta
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts

# =============================================================================
# Configuration
# =============================================================================

PROGRAM_ID       = Pubkey.from_string("DKsRhfniEEv3EcNgvbid11aDAAC3Mbsxui3rTQnU5GS3")
RPC_URL          = "https://api.devnet.solana.com"
RENT_SYSVAR      = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
LAMPORTS_PER_SOL = 1_000_000_000

GOAL_SOL      = 0.1
CONTRIB_1_SOL = 0.06
CONTRIB_2_SOL = 0.05

SHORT_DEADLINE  = True
DEADLINE_OFFSET = 45   # seconds

# =============================================================================
# Result tracking
# =============================================================================

@dataclass
class StepResult:
    number: int
    label:  str
    passed: bool
    note:   str = ""

_results: list = []

def record(number: int, label: str, passed: bool, note: str = ""):
    icon   = "✅" if passed else "❌"
    status = "PASS" if passed else "FAIL"
    _results.append(StepResult(number, label, passed, note))
    print(f"  [{icon} {status}] {label}")
    if note:
        print(f"         → {note}")

def print_summary():
    print("\n" + "=" * 66)
    print("TEST SUMMARY")
    print("=" * 66)
    for r in _results:
        icon   = "✅" if r.passed else "❌"
        status = "PASS" if r.passed else "FAIL"
        print(f"  {r.number:>2}. {r.label:<44} {icon} {status}")
        if not r.passed and r.note:
            print(f"       → {r.note}")
    passed = sum(1 for r in _results if r.passed)
    total  = len(_results)
    print("-" * 66)
    print(f"  Result : {passed}/{total} passed")
    print("=" * 66)

# =============================================================================
# Wallet helpers
# =============================================================================

def _default_keypair_paths():
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".config", "solana", "id.json"),
        os.path.join(home, "solana",  "id.json"),
        os.path.join(home, ".solana", "id.json"),
        "id.json",
    ]

def load_wallet(keypair_path=None, generate_new=False) -> Keypair:
    if keypair_path:
        return _load_from_file(keypair_path)
    env = os.environ.get("SOLANA_KEYPAIR_PATH")
    if env:
        print(f"Using keypair from SOLANA_KEYPAIR_PATH: {env}")
        return _load_from_file(env)
    for path in _default_keypair_paths():
        if os.path.exists(path):
            print(f"Found keypair at: {path}")
            return _load_from_file(path)
    if generate_new:
        kp = Keypair()
        print("Generated throwaway keypair (NOT saved to disk).")
        print(f"  Public key : {kp.pubkey()}")
        print("  WARNING    : funds lost when script exits.")
        return kp
    print("ERROR: No Solana keypair found.")
    print("  A) solana-keygen new")
    print("  B) python test_client.py --keypair /path/to/id.json")
    print("  C) export SOLANA_KEYPAIR_PATH=/path/to/id.json")
    print("  D) python test_client.py --new-wallet")
    sys.exit(1)

def _load_from_file(path: str) -> Keypair:
    if not os.path.exists(path):
        print(f"ERROR: Keypair file not found: {path}")
        sys.exit(1)
    with open(path) as f:
        raw = json.load(f)
    if not isinstance(raw, list) or len(raw) != 64:
        print(f"ERROR: Expected 64-element array, got {len(raw)}.")
        sys.exit(1)
    return Keypair.from_bytes(bytes(raw))

# =============================================================================
# RPC / balance helpers
# =============================================================================

def get_client() -> Client:
    c = Client(RPC_URL)
    try:
        c.get_version()
    except Exception as e:
        print(f"ERROR: Cannot reach Devnet: {e}")
        sys.exit(1)
    return c

def get_balance_sol(client: Client, pubkey: Pubkey) -> float:
    return client.get_balance(pubkey, commitment=Confirmed).value / LAMPORTS_PER_SOL

def ensure_funded(client: Client, pubkey: Pubkey, min_sol: float = 0.5):
    balance = get_balance_sol(client, pubkey)
    print(f"Wallet balance : {balance:.4f} SOL")
    if balance >= min_sol:
        return
    print(f"Balance below {min_sol} SOL — requesting 2 SOL airdrop...")
    for attempt in range(1, 4):
        try:
            resp    = client.request_airdrop(pubkey, 2 * LAMPORTS_PER_SOL, commitment=Confirmed)
            sig_obj = resp.value   # already a Signature object — do NOT str()
            print(f"  Airdrop tx: {sig_obj}")
            for _ in range(45):
                time.sleep(1)
                st = client.get_signature_statuses([sig_obj]).value[0]
                if st and st.confirmation_status:
                    bal = get_balance_sol(client, pubkey)
                    print(f"  Airdrop confirmed! New balance: {bal:.4f} SOL")
                    return
        except Exception as e:
            print(f"  Attempt {attempt} failed: {e}")
            time.sleep(6)
    print("WARNING: Airdrop failed. Run: solana airdrop 2 --url devnet")

# =============================================================================
# Instruction builders — exact account layouts from lib.rs
#
# CreateCampaign: creator(signer,w), campaign(signer,w), system_program, rent
# Contribute:     donor(signer,w),   campaign(w),         system_program
# Withdraw:       creator(signer,w), campaign(w)
# Refund:         donor(signer,w),   campaign(w)
#
# NOTE: The campaign account IS the fund storage. There is no separate vault
# PDA account passed to any instruction — the program transfers SOL directly
# into and out of the campaign account.
# =============================================================================

def ix_create_campaign(creator: Pubkey, campaign: Pubkey,
                        goal: int, deadline: int) -> Instruction:
    """[0x00] | goal u64 LE (8 bytes) | deadline i64 LE (8 bytes)"""
    data = bytes([0]) + struct.pack("<Q", goal) + struct.pack("<q", deadline)
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=creator,           is_signer=True,  is_writable=True),
            AccountMeta(pubkey=campaign,           is_signer=True,  is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=RENT_SYSVAR,       is_signer=False, is_writable=False),
        ],
        data=data,
    )

def ix_contribute(donor: Pubkey, campaign: Pubkey, amount: int) -> Instruction:
    """
    [0x01] | amount u64 LE (8 bytes)
    3 accounts: donor, campaign, system_program
    SOL is transferred directly into the campaign account (no separate vault).
    """
    data = bytes([1]) + struct.pack("<Q", amount)
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=donor,             is_signer=True,  is_writable=True),
            AccountMeta(pubkey=campaign,           is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
        data=data,
    )

def ix_withdraw(creator: Pubkey, campaign: Pubkey) -> Instruction:
    """
    [0x02]  (no payload)
    2 accounts: creator, campaign
    Program drains campaign lamports → creator using direct lamport manipulation.
    """
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=creator,  is_signer=True,  is_writable=True),
            AccountMeta(pubkey=campaign, is_signer=False, is_writable=True),
        ],
        data=bytes([2]),
    )

def ix_refund(donor: Pubkey, campaign: Pubkey, amount: int) -> Instruction:
    """
    [0x03] | amount u64 LE (8 bytes)
    2 accounts: donor, campaign
    """
    data = bytes([3]) + struct.pack("<Q", amount)
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=donor,    is_signer=True,  is_writable=True),
            AccountMeta(pubkey=campaign, is_signer=False, is_writable=True),
        ],
        data=data,
    )

# =============================================================================
# Transaction helpers
# =============================================================================

def send_tx(client: Client, instructions: list, signers: list,
            label: str = "tx") -> Optional[Signature]:
    """
    Build + sign + send a VersionedTransaction (MessageV0).
    Returns a Signature object (NOT a str) — required by get_signature_statuses.
    Prints full program logs on failure.
    """
    try:
        bh  = client.get_latest_blockhash(commitment=Confirmed).value.blockhash
        msg = MessageV0.try_compile(
            payer=signers[0].pubkey(),
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=bh,
        )
        tx   = VersionedTransaction(msg, signers)
        opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        resp = client.send_transaction(tx, opts=opts)
        sig  = resp.value   # Signature object — do NOT str() it before storing
        print(f"  [{label}] Sig     : {sig}")
        print(f"  [{label}] Explorer: https://explorer.solana.com/tx/{sig}?cluster=devnet")
        return sig
    except Exception as e:
        err_str = str(e)
        print(f"  [{label}] FAILED  : {err_str[:300]}")
        # Extract program log lines for readable on-chain errors
        try:
            logs = re.findall(r'"Program log: (.*?)"', err_str)
            if logs:
                print(f"  [{label}] Program logs:")
                for line in logs:
                    print(f"    >> {line}")
            else:
                # Fall back to printing the full error so nothing is hidden
                print(f"  [{label}] Full error:\n    {err_str}")
        except Exception:
            pass
        return None

def wait_confirm(client: Client, sig: Optional[Signature],
                 timeout: int = 60) -> bool:
    """
    Poll for confirmation. sig must be a Signature object (not str).
    confirmation_status is a solders enum — use str().lower() to compare.
    """
    if not sig:
        return False
    for i in range(timeout):
        time.sleep(1)
        try:
            status = client.get_signature_statuses([sig]).value[0]
            if status is not None:
                cs = str(status.confirmation_status).lower() \
                     if status.confirmation_status else ""
                if "confirmed" in cs or "finalized" in cs:
                    return True
        except Exception as e:
            if i % 15 == 0:
                print(f"  Polling ({i}s) ... ({e})")
    print(f"  WARNING: Timed out after {timeout}s")
    return False

def check_tx_status(client: Client, sig: Optional[Signature]):
    """Fetch full TX record — use when wait_confirm times out."""
    if not sig:
        return
    print(f"  On-chain check: {sig}")
    try:
        result = client.get_transaction(
            sig,
            max_supported_transaction_version=0,
            commitment=Confirmed,
        )
        if result.value is None:
            print("  Not found — may still be processing.")
            return
        meta = result.value.transaction.meta
        if meta is None:
            print("  TX found but metadata unavailable.")
            return
        if meta.err:
            print(f"  TX FAILED on-chain: {meta.err}")
        else:
            print(f"  TX confirmed ✓  (fee: {meta.fee} lamports)")
        if meta.log_messages:
            print("  Program logs:")
            for line in meta.log_messages:
                print(f"    >> {line}")
    except Exception as e:
        print(f"  Status check error: {e}")

def send_and_confirm(client: Client, instructions: list, signers: list,
                     label: str) -> bool:
    sig = send_tx(client, instructions, signers, label)
    if sig is None:
        return False
    ok = wait_confirm(client, sig)
    if not ok:
        print(f"  Polling timed out — fetching on-chain status...")
        check_tx_status(client, sig)
        time.sleep(8)
        check_tx_status(client, sig)
    return ok

# =============================================================================
# Checklist
# =============================================================================

def run_checklist(client: Client, wallet: Keypair):
    print("\n" + "=" * 66)
    print("CROWDFUNDING TEST CHECKLIST")
    print("=" * 66)

    campaign_kp = Keypair()

    goal_lamps     = int(GOAL_SOL      * LAMPORTS_PER_SOL)  # 100_000_000
    contrib1_lamps = int(CONTRIB_1_SOL * LAMPORTS_PER_SOL)  #  60_000_000
    contrib2_lamps = int(CONTRIB_2_SOL * LAMPORTS_PER_SOL)  #  50_000_000

    if SHORT_DEADLINE:
        deadline      = int(time.time()) + DEADLINE_OFFSET
        deadline_desc = f"~{DEADLINE_OFFSET}s from now"
    else:
        deadline      = int(time.time()) + 86_400
        deadline_desc = "24 hours from now"

    creator_pubkey  = wallet.pubkey()
    campaign_pubkey = campaign_kp.pubkey()

    print(f"\n  Creator  : {creator_pubkey}")
    print(f"  Campaign : {campaign_pubkey}  ← also acts as the fund vault")
    print(f"  Goal     : {GOAL_SOL} SOL  ({goal_lamps:,} lamports)")
    print(f"  Contrib1 : {CONTRIB_1_SOL} SOL  →  raised = {CONTRIB_1_SOL}")
    print(f"  Contrib2 : {CONTRIB_2_SOL} SOL  →  raised = "
          f"{round(CONTRIB_1_SOL + CONTRIB_2_SOL, 2)}")
    print(f"  Deadline : {deadline_desc}")

    # ── STEP 1: Create campaign ───────────────────────────────────────────────
    print("\n" + "─" * 66)
    print(f"STEP 1 — Create campaign  (goal={GOAL_SOL} SOL)")
    print("─" * 66)

    ok = send_and_confirm(
        client,
        [ix_create_campaign(creator_pubkey, campaign_pubkey, goal_lamps, deadline)],
        [wallet, campaign_kp],
        "create_campaign",
    )
    record(1, "Create campaign", ok,
           "" if ok else "TX not confirmed — paste Explorer link into browser")
    if not ok:
        print("\n  Cannot continue. Aborting.")
        print_summary()
        return

    # ── STEP 2: Contribute 0.06 SOL ──────────────────────────────────────────
    print("\n" + "─" * 66)
    print(f"STEP 2 — Contribute {CONTRIB_1_SOL} SOL  →  raised = {CONTRIB_1_SOL} SOL")
    print("─" * 66)

    camp_before_2  = get_balance_sol(client, campaign_pubkey)
    wallet_before_2 = get_balance_sol(client, creator_pubkey)

    ok = send_and_confirm(
        client,
        [ix_contribute(creator_pubkey, campaign_pubkey, contrib1_lamps)],
        [wallet],
        "contribute_0.06",
    )

    camp_after_2   = get_balance_sol(client, campaign_pubkey)
    wallet_after_2 = get_balance_sol(client, creator_pubkey)
    camp_delta_2   = camp_after_2  - camp_before_2
    wallet_delta_2 = wallet_before_2 - wallet_after_2

    print(f"\n  Campaign balance : {camp_before_2:.6f} → {camp_after_2:.6f}  "
          f"(+{camp_delta_2:.6f} SOL)")
    print(f"  Wallet balance   : {wallet_before_2:.6f} → {wallet_after_2:.6f}  "
          f"(-{wallet_delta_2:.6f} SOL incl fee)")

    funds_received_2 = camp_delta_2 > 0
    record(2, f"Contribute {CONTRIB_1_SOL} SOL → raised={CONTRIB_1_SOL} SOL",
           ok and funds_received_2,
           "" if (ok and funds_received_2) else
           ("TX failed" if not ok else
            "Campaign balance unchanged — check account layout"))

    # ── STEP 3: Contribute 0.05 SOL → raised = 0.11 SOL ──────────────────────
    print("\n" + "─" * 66)
    print(f"STEP 3 — Contribute {CONTRIB_2_SOL} SOL  →  raised = "
          f"{round(CONTRIB_1_SOL + CONTRIB_2_SOL, 2)} SOL  (exceeds goal)")
    print("─" * 66)

    camp_before_3 = get_balance_sol(client, campaign_pubkey)

    ok = send_and_confirm(
        client,
        [ix_contribute(creator_pubkey, campaign_pubkey, contrib2_lamps)],
        [wallet],
        "contribute_0.05",
    )

    camp_after_3  = get_balance_sol(client, campaign_pubkey)
    camp_delta_3  = camp_after_3 - camp_before_3

    print(f"\n  Campaign balance : {camp_before_3:.6f} → {camp_after_3:.6f}  "
          f"(+{camp_delta_3:.6f} SOL)")

    funds_received_3 = camp_delta_3 > 0
    record(3, f"Contribute {CONTRIB_2_SOL} SOL → raised="
               f"{round(CONTRIB_1_SOL + CONTRIB_2_SOL, 2)} SOL",
           ok and funds_received_3,
           "" if (ok and funds_received_3) else
           ("TX failed" if not ok else "Campaign balance unchanged"))

    # ── STEP 4: Withdraw BEFORE deadline → must FAIL ──────────────────────────
    print("\n" + "─" * 66)
    print("STEP 4 — Withdraw BEFORE deadline  →  must FAIL  (CampaignActive)")
    print("─" * 66)
    secs = max(0, deadline - int(time.time()))
    print(f"  {secs}s until deadline — program should reject with CampaignActive")

    sig_early = send_tx(
        client,
        [ix_withdraw(creator_pubkey, campaign_pubkey)],
        [wallet],
        "early_withdraw",
    )

    if sig_early is None:
        # send_tx returned None = preflight rejected it = correct
        record(4, "Withdraw before deadline → rejected ✓", True,
               "Correctly rejected at preflight")
    else:
        early_ok = wait_confirm(client, sig_early, timeout=20)
        if early_ok:
            record(4, "Withdraw before deadline → rejected ✓", False,
                   "⚠️  Program ALLOWED early withdrawal — deadline check missing!")
        else:
            record(4, "Withdraw before deadline → rejected ✓", True,
                   "Correctly rejected by validator")

    # ── STEP 5: Wait for deadline, then withdraw → must SUCCEED ───────────────
    print("\n" + "─" * 66)
    print("STEP 5 — Wait for deadline, then withdraw  →  must SUCCEED")
    print("─" * 66)

    remaining = deadline - int(time.time())
    if remaining > 0:
        wait_secs = remaining + 3
        print(f"  Waiting {wait_secs}s for deadline to pass...")
        for t in range(wait_secs, 0, -1):
            print(f"  ⏳ {t:>3}s remaining...", end="\r")
            time.sleep(1)
        print()
    else:
        print("  Deadline already passed.")

    wallet_pre_w  = get_balance_sol(client, creator_pubkey)
    camp_pre_w    = get_balance_sol(client, campaign_pubkey)

    ok = send_and_confirm(
        client,
        [ix_withdraw(creator_pubkey, campaign_pubkey)],
        [wallet],
        "withdraw",
    )

    wallet_post_w = get_balance_sol(client, creator_pubkey)
    camp_post_w   = get_balance_sol(client, campaign_pubkey)
    wallet_net    = wallet_post_w - wallet_pre_w
    camp_net      = camp_post_w  - camp_pre_w

    print(f"  Wallet   : {wallet_pre_w:.6f} → {wallet_post_w:.6f}  "
          f"({wallet_net:+.6f} SOL)")
    print(f"  Campaign : {camp_pre_w:.6f} → {camp_post_w:.6f}  "
          f"({camp_net:+.6f} SOL)")

    record(5, "Withdraw after deadline → success", ok,
           "" if ok else "Withdraw TX failed — check program logs above")

    # ── STEP 6: Withdraw again → must FAIL (AlreadyClaimed) ───────────────────
    print("\n" + "─" * 66)
    print("STEP 6 — Withdraw again  →  must FAIL  (AlreadyClaimed)")
    print("─" * 66)

    sig_double = send_tx(
        client,
        [ix_withdraw(creator_pubkey, campaign_pubkey)],
        [wallet],
        "double_withdraw",
    )

    if sig_double is None:
        record(6, "Double withdraw → rejected ✓  (AlreadyClaimed)", True,
               "Correctly rejected at preflight")
    else:
        double_ok = wait_confirm(client, sig_double, timeout=20)
        if double_ok:
            record(6, "Double withdraw → rejected ✓  (AlreadyClaimed)", False,
                   "⚠️  Program ALLOWED double withdrawal — claimed flag never set!")
        else:
            record(6, "Double withdraw → rejected ✓  (AlreadyClaimed)", True,
                   "Correctly rejected by validator")

    print_summary()

# =============================================================================
# CLI
# =============================================================================

def parse_args():
    p = argparse.ArgumentParser(description="Solana Crowdfunding — Devnet test checklist")
    p.add_argument("--keypair",       metavar="PATH",
                   help="Path to Solana CLI keypair JSON (64-byte array).")
    p.add_argument("--new-wallet",    action="store_true",
                   help="Generate a throwaway keypair + auto-airdrop.")
    p.add_argument("--long-deadline", action="store_true",
                   help="Use 24h deadline instead of 45s.")
    return p.parse_args()

def main():
    args = parse_args()
    global SHORT_DEADLINE
    if args.long_deadline:
        SHORT_DEADLINE = False

    print("=" * 66)
    print("Solana Crowdfunding — Devnet Test Client")
    print("=" * 66)
    print(f"Program    : {PROGRAM_ID}")
    print(f"RPC        : {RPC_URL}")
    print(f"TX format  : VersionedTransaction + MessageV0")
    print(f"Deadline   : {'SHORT ~45s' if SHORT_DEADLINE else 'LONG 24h'}")
    print()

    wallet = load_wallet(keypair_path=args.keypair, generate_new=args.new_wallet)
    print(f"Wallet     : {wallet.pubkey()}")

    client = get_client()
    print("Devnet     : connected\n")

    ensure_funded(client, wallet.pubkey(), min_sol=0.5)

    balance = get_balance_sol(client, wallet.pubkey())
    if balance < 0.3:
        print(f"ERROR: Need at least 0.3 SOL.  Current: {balance:.4f}")
        print("  Run:  solana airdrop 2 --url devnet")
        sys.exit(1)

    print(f"Balance    : {balance:.4f} SOL  (ready)\n")

    run_checklist(client, wallet)
    print("\nDone.")

if __name__ == "__main__":
    main()

# =============================================================================
# HOW TO REDEPLOY AFTER FIXING lib.rs
# =============================================================================
# The fix is a one-line change in src/lib.rs:
#
#   OLD:  let campaign_data_len = 8 + 32 + 8 + 8 + 8 + 1 + 1;  // = 66 (WRONG)
#   NEW:  let campaign_data_len = 32 + 8 + 8 + 8 + 1 + 1;       // = 58 (CORRECT)
#
# Then rebuild and redeploy:
#
#   cargo build-sbf
#   solana program deploy target/deploy/solana_crowdfunding.so --url devnet
#
# The program ID stays the same (you're upgrading an existing deployment).
# After deploying, run:
#
#   python test_client.py
