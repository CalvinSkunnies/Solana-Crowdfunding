"""
Solana Crowdfunding Platform — Python Test Client
Devnet · PDA Vault edition

Usage:
    pip install solders solana

    python test_client.py                           # use ~/.config/solana/id.json
    python test_client.py --keypair /path/id.json   # explicit keypair
    python test_client.py --new-wallet              # throwaway keypair + auto-airdrop
    python test_client.py --scenario success        # run one scenario
    python test_client.py --scenario refund
    python test_client.py --scenario all            # default

Architecture
────────────
Each campaign has TWO accounts:

  campaign account  (owned by the program, holds Campaign state / raised counter)
  vault PDA         (seeds: ["vault", campaign_pubkey], system-owned, holds all SOL)

Donations go into the vault PDA — never into the campaign account directly.
The program signs vault transfers with invoke_signed using the bump stored in
Campaign.bump.

Rebuild & redeploy after editing lib.rs:
    cargo build-sbf
    solana program deploy target/deploy/solana_crowdfunding.so --url devnet
"""

import argparse
import json
import os
import re
import struct
import sys
import time
from dataclasses import dataclass
from typing import Optional, List

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

PROGRAM_ID        = Pubkey.from_string("DKsRhfniEEv3EcNgvbid11aDAAC3Mbsxui3rTQnU5GS3")
RPC_URL           = "https://api.devnet.solana.com"
RENT_SYSVAR       = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
LAMPORTS_PER_SOL  = 1_000_000_000

GOAL_SOL      = 0.10
CONTRIB_1_SOL = 0.06
CONTRIB_2_SOL = 0.05
DEADLINE_SECS = 45   # short deadline so tests finish fast

# =============================================================================
# PDA helpers
# =============================================================================

def find_vault_pda(campaign_pubkey: Pubkey) -> tuple:
    """
    Derive the vault PDA — mirrors lib.rs find_vault_pda().
    Seeds: [b"vault", campaign_pubkey]
    Returns (Pubkey, bump: int).
    """
    seeds = [b"vault", bytes(campaign_pubkey)]
    return Pubkey.find_program_address(seeds, PROGRAM_ID)

# =============================================================================
# Wallet helpers
# =============================================================================

def _default_keypair_paths() -> List[str]:
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".config", "solana", "id.json"),
        os.path.join(home, "solana", "id.json"),
        os.path.join(home, ".solana", "id.json"),
        "id.json",
    ]

def load_wallet(keypair_path: Optional[str] = None, generate_new: bool = False) -> Keypair:
    """
    Load a Keypair from (in priority order):
      1. --keypair argument
      2. SOLANA_KEYPAIR_PATH env var
      3. Default Solana CLI path (OS-aware)
      4. Generate throwaway (--new-wallet)
    """
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
        print(f"ERROR: Cannot reach Devnet ({RPC_URL}): {e}")
        sys.exit(1)
    return c

def get_balance_sol(client: Client, pubkey: Pubkey) -> float:
    return client.get_balance(pubkey, commitment=Confirmed).value / LAMPORTS_PER_SOL

def ensure_funded(client: Client, pubkey: Pubkey, min_sol: float = 0.5):
    """Auto-airdrop 2 SOL if balance is below min_sol."""
    balance = get_balance_sol(client, pubkey)
    print(f"Wallet balance : {balance:.4f} SOL")
    if balance >= min_sol:
        return
    print(f"Balance below {min_sol} SOL — requesting 2 SOL airdrop...")
    for attempt in range(1, 4):
        try:
            resp = client.request_airdrop(pubkey, 2 * LAMPORTS_PER_SOL, commitment=Confirmed)
            sig  = resp.value
            print(f"  Airdrop TX: {sig}")
            for _ in range(45):
                time.sleep(1)
                st = client.get_signature_statuses([sig]).value[0]
                if st and st.confirmation_status:
                    balance = get_balance_sol(client, pubkey)
                    print(f"  Confirmed! New balance: {balance:.4f} SOL")
                    return
        except Exception as e:
            print(f"  Attempt {attempt} failed: {e}")
            time.sleep(6)
    print("WARNING: Airdrop failed. Run: solana airdrop 2 --url devnet")

# =============================================================================
# Transaction helpers
# =============================================================================

def send_tx(
    client: Client,
    instructions: List[Instruction],
    signers: List[Keypair],
    label: str = "tx",
) -> Optional[Signature]:
    """Build, sign and send a VersionedTransaction. Returns Signature or None."""
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
        sig  = resp.value
        print(f"  [{label}] sig      : {sig}")
        print(f"  [{label}] explorer : https://explorer.solana.com/tx/{sig}?cluster=devnet")
        return sig
    except Exception as e:
        err = str(e)
        print(f"  [{label}] FAILED   : {err[:400]}")
        logs = re.findall(r'"Program log: (.*?)"', err)
        if logs:
            print(f"  [{label}] program logs:")
            for line in logs:
                print(f"    >> {line}")
        return None

def wait_confirm(client: Client, sig: Optional[Signature], timeout: int = 60) -> bool:
    if not sig:
        return False
    for i in range(timeout):
        time.sleep(1)
        try:
            status = client.get_signature_statuses([sig]).value[0]
            if status is not None:
                cs = str(status.confirmation_status).lower() if status.confirmation_status else ""
                if "confirmed" in cs or "finalized" in cs:
                    return True
        except Exception:
            pass
    print(f"  WARNING: Timed out after {timeout}s")
    return False

def send_and_confirm(client, instructions, signers, label) -> bool:
    sig = send_tx(client, instructions, signers, label)
    if sig is None:
        return False
    return wait_confirm(client, sig)

# =============================================================================
# Instruction builders
#
# All instructions now include the vault PDA as an explicit account.
# vault = find_vault_pda(campaign_pubkey)[0]   ← compute this before calling
#
# Instruction | Accounts (in order)
# ------------|---------------------------------------------------
# create      | creator(sw), campaign(sw), vault(w), system, rent
# contribute  | donor(sw),   campaign(w),  vault(w), system
# withdraw    | creator(sw), campaign(w),  vault(w), system
# refund      | donor(sw),   campaign(w),  vault(w), system
# =============================================================================

def ix_create_campaign(
    creator: Pubkey, campaign: Pubkey, vault: Pubkey,
    goal: int, deadline: int,
) -> Instruction:
    """[0x00] | goal(u64 LE) | deadline(i64 LE)"""
    data = bytes([0]) + struct.pack("<Q", goal) + struct.pack("<q", deadline)
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=creator,           is_signer=True,  is_writable=True),
            AccountMeta(pubkey=campaign,           is_signer=True,  is_writable=True),
            AccountMeta(pubkey=vault,              is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=RENT_SYSVAR,        is_signer=False, is_writable=False),
        ],
        data=data,
    )

def ix_contribute(
    donor: Pubkey, campaign: Pubkey, vault: Pubkey, amount: int,
) -> Instruction:
    """[0x01] | amount(u64 LE) — SOL goes into vault PDA, NOT campaign account"""
    data = bytes([1]) + struct.pack("<Q", amount)
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=donor,             is_signer=True,  is_writable=True),
            AccountMeta(pubkey=campaign,           is_signer=False, is_writable=True),
            AccountMeta(pubkey=vault,              is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
        data=data,
    )

def ix_withdraw(
    creator: Pubkey, campaign: Pubkey, vault: Pubkey,
) -> Instruction:
    """[0x02] — drains vault → creator via invoke_signed"""
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=creator,           is_signer=True,  is_writable=True),
            AccountMeta(pubkey=campaign,           is_signer=False, is_writable=True),
            AccountMeta(pubkey=vault,              is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
        data=bytes([2]),
    )

def ix_refund(
    donor: Pubkey, campaign: Pubkey, vault: Pubkey, amount: int,
) -> Instruction:
    """[0x03] | amount(u64 LE) — vault → donor via invoke_signed"""
    data = bytes([3]) + struct.pack("<Q", amount)
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=donor,             is_signer=True,  is_writable=True),
            AccountMeta(pubkey=campaign,           is_signer=False, is_writable=True),
            AccountMeta(pubkey=vault,              is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
        data=data,
    )

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
# Scenario 1 — goal reached → withdraw
# =============================================================================

def run_success_scenario(client: Client, wallet: Keypair):
    print("\n" + "=" * 66)
    print("SCENARIO 1: Goal Reached → Withdraw")
    print("=" * 66)

    campaign_kp  = Keypair()
    campaign_pub = campaign_kp.pubkey()
    vault_pub, _ = find_vault_pda(campaign_pub)

    goal_lamps     = int(GOAL_SOL      * LAMPORTS_PER_SOL)
    contrib1_lamps = int(CONTRIB_1_SOL * LAMPORTS_PER_SOL)
    contrib2_lamps = int(CONTRIB_2_SOL * LAMPORTS_PER_SOL)
    deadline       = int(time.time()) + DEADLINE_SECS
    creator_pub    = wallet.pubkey()

    print(f"\n  Creator  : {creator_pub}")
    print(f"  Campaign : {campaign_pub}   ← state account")
    print(f"  Vault PDA: {vault_pub}   ← SOL stored here")
    print(f"  Goal     : {GOAL_SOL} SOL")
    print(f"  Deadline : ~{DEADLINE_SECS}s from now")

    # Step 1 — Create
    print("\n─── STEP 1: Create campaign")
    ok = send_and_confirm(
        client,
        [ix_create_campaign(creator_pub, campaign_pub, vault_pub, goal_lamps, deadline)],
        [wallet, campaign_kp],
        "create_campaign",
    )
    record(1, f"Create campaign (goal={GOAL_SOL} SOL)", ok)
    if not ok:
        return

    vault_rent_bal = get_balance_sol(client, vault_pub)
    print(f"\n  Vault balance after create: {vault_rent_bal:.6f} SOL (rent-exempt minimum)")

    # Step 2 — Contribute 0.06
    print(f"\n─── STEP 2: Contribute {CONTRIB_1_SOL} SOL → vault")
    vault_before = get_balance_sol(client, vault_pub)
    ok = send_and_confirm(
        client,
        [ix_contribute(creator_pub, campaign_pub, vault_pub, contrib1_lamps)],
        [wallet], "contribute_1",
    )
    vault_after = get_balance_sol(client, vault_pub)
    delta = vault_after - vault_before
    print(f"\n  Vault: {vault_before:.6f} → {vault_after:.6f} SOL  (+{delta:.6f})")
    record(2, f"Contribute {CONTRIB_1_SOL} SOL → vault received funds",
           ok and delta >= CONTRIB_1_SOL * 0.99)

    # Step 3 — Contribute 0.05 (total 0.11 > goal)
    print(f"\n─── STEP 3: Contribute {CONTRIB_2_SOL} SOL (total > goal)")
    vault_before = get_balance_sol(client, vault_pub)
    ok = send_and_confirm(
        client,
        [ix_contribute(creator_pub, campaign_pub, vault_pub, contrib2_lamps)],
        [wallet], "contribute_2",
    )
    vault_after = get_balance_sol(client, vault_pub)
    delta = vault_after - vault_before
    print(f"\n  Vault: {vault_before:.6f} → {vault_after:.6f} SOL  (+{delta:.6f})")
    record(3, f"Contribute {CONTRIB_2_SOL} SOL → vault > goal",
           ok and delta >= CONTRIB_2_SOL * 0.99)

    # Step 4 — Early withdraw (must fail)
    print("\n─── STEP 4: Withdraw before deadline (expect: FAIL)")
    secs = max(0, deadline - int(time.time()))
    print(f"  {secs}s until deadline — program must reject")
    sig_early = send_tx(client, [ix_withdraw(creator_pub, campaign_pub, vault_pub)],
                        [wallet], "early_withdraw")
    if sig_early is None:
        record(4, "Withdraw before deadline → rejected ✓", True, "Rejected at preflight")
    else:
        early_ok = wait_confirm(client, sig_early, timeout=20)
        record(4, "Withdraw before deadline → rejected ✓", not early_ok,
               "Correctly rejected" if not early_ok else
               "⚠️  Program allowed early withdrawal!")

    # Step 5 — Wait, then withdraw (must succeed)
    print("\n─── STEP 5: Wait for deadline then withdraw (expect: SUCCESS)")
    remaining = deadline - int(time.time())
    if remaining > 0:
        wait = remaining + 3
        print(f"  Waiting {wait}s...")
        for t in range(wait, 0, -1):
            print(f"  ⏳ {t:>3}s ...", end="\r")
            time.sleep(1)
        print()

    vault_pre   = get_balance_sol(client, vault_pub)
    creator_pre = get_balance_sol(client, creator_pub)
    ok = send_and_confirm(
        client,
        [ix_withdraw(creator_pub, campaign_pub, vault_pub)],
        [wallet], "withdraw",
    )
    vault_post   = get_balance_sol(client, vault_pub)
    creator_post = get_balance_sol(client, creator_pub)
    print(f"\n  Vault   : {vault_pre:.6f} → {vault_post:.6f} SOL ({vault_post-vault_pre:+.6f})")
    print(f"  Creator : {creator_pre:.6f} → {creator_post:.6f} SOL ({creator_post-creator_pre:+.6f})")
    record(5, "Withdraw after deadline → success", ok and vault_post < vault_pre)

    # Step 6 — Double-withdraw (must fail)
    print("\n─── STEP 6: Double-withdraw (expect: FAIL — AlreadyClaimed)")
    sig_double = send_tx(client, [ix_withdraw(creator_pub, campaign_pub, vault_pub)],
                         [wallet], "double_withdraw")
    if sig_double is None:
        record(6, "Double-withdraw → rejected ✓", True, "Rejected at preflight")
    else:
        double_ok = wait_confirm(client, sig_double, timeout=20)
        record(6, "Double-withdraw → rejected ✓", not double_ok,
               "Correctly rejected" if not double_ok else
               "⚠️  Program allowed double withdrawal!")

# =============================================================================
# Scenario 2 — goal not reached → refund
# =============================================================================

def run_refund_scenario(client: Client, wallet: Keypair):
    print("\n" + "=" * 66)
    print("SCENARIO 2: Goal NOT Reached → Refund")
    print("=" * 66)

    campaign_kp  = Keypair()
    campaign_pub = campaign_kp.pubkey()
    vault_pub, _ = find_vault_pda(campaign_pub)

    goal_lamps    = int(0.50 * LAMPORTS_PER_SOL)
    contrib_lamps = int(0.05 * LAMPORTS_PER_SOL)
    deadline      = int(time.time()) + DEADLINE_SECS
    creator_pub   = wallet.pubkey()

    print(f"\n  Creator  : {creator_pub}")
    print(f"  Campaign : {campaign_pub}")
    print(f"  Vault PDA: {vault_pub}")
    print(f"  Goal     : 0.5 SOL  (we contribute only 0.05 — goal not met)")

    # Create
    print("\n─── STEP 1: Create campaign")
    ok = send_and_confirm(
        client,
        [ix_create_campaign(creator_pub, campaign_pub, vault_pub, goal_lamps, deadline)],
        [wallet, campaign_kp],
        "create_campaign",
    )
    record(7, "Create campaign (goal=0.5 SOL)", ok)
    if not ok:
        return

    # Contribute (far below goal)
    print(f"\n─── STEP 2: Contribute 0.05 SOL")
    ok = send_and_confirm(
        client,
        [ix_contribute(creator_pub, campaign_pub, vault_pub, contrib_lamps)],
        [wallet], "contribute",
    )
    vault_bal = get_balance_sol(client, vault_pub)
    print(f"\n  Vault balance: {vault_bal:.6f} SOL")
    record(8, "Contribute 0.05 SOL → vault holds funds", ok and vault_bal > 0)

    # Wait for deadline then try withdraw (must fail — goal not met)
    print("\n─── STEP 3: Wait + withdraw (expect: FAIL — GoalNotReached)")
    remaining = deadline - int(time.time())
    if remaining > 0:
        wait = remaining + 3
        print(f"  Waiting {wait}s...")
        for t in range(wait, 0, -1):
            print(f"  ⏳ {t:>3}s ...", end="\r")
            time.sleep(1)
        print()
    sig_wd = send_tx(client, [ix_withdraw(creator_pub, campaign_pub, vault_pub)],
                     [wallet], "withdraw_no_goal")
    if sig_wd is None:
        record(9, "Withdraw with goal not met → rejected ✓", True, "Rejected at preflight")
    else:
        wd_ok = wait_confirm(client, sig_wd, timeout=20)
        record(9, "Withdraw with goal not met → rejected ✓", not wd_ok,
               "Correctly rejected" if not wd_ok else
               "⚠️  Program allowed withdrawal despite goal not reached!")

    # Refund (must succeed)
    print(f"\n─── STEP 4: Refund 0.05 SOL (expect: SUCCESS)")
    vault_pre   = get_balance_sol(client, vault_pub)
    creator_pre = get_balance_sol(client, creator_pub)
    ok = send_and_confirm(
        client,
        [ix_refund(creator_pub, campaign_pub, vault_pub, contrib_lamps)],
        [wallet], "refund",
    )
    vault_post   = get_balance_sol(client, vault_pub)
    creator_post = get_balance_sol(client, creator_pub)
    print(f"\n  Vault   : {vault_pre:.6f} → {vault_post:.6f} SOL")
    print(f"  Creator : {creator_pre:.6f} → {creator_post:.6f} SOL")
    record(10, "Refund donor → success", ok and vault_post < vault_pre)

# =============================================================================
# CLI
# =============================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="Solana Crowdfunding — Devnet test client (PDA Vault edition)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--keypair",    metavar="PATH",
                   help="Path to a Solana CLI keypair JSON (64-byte array).")
    p.add_argument("--new-wallet", action="store_true",
                   help="Generate a throwaway keypair for this run.")
    p.add_argument("--scenario",   choices=["success", "refund", "all"], default="all",
                   help="Scenario to run (default: all).")
    return p.parse_args()

def main():
    args = parse_args()

    print("=" * 66)
    print("Solana Crowdfunding — Devnet Test Client  [PDA Vault Edition]")
    print("=" * 66)
    print(f"Program    : {PROGRAM_ID}")
    print(f"RPC        : {RPC_URL}")
    print(f"TX format  : VersionedTransaction + MessageV0")
    print(f"Vault seed : [b'vault', campaign_pubkey]")
    print()

    wallet = load_wallet(keypair_path=args.keypair, generate_new=args.new_wallet)
    print(f"Wallet     : {wallet.pubkey()}")

    client = get_client()
    print("Devnet     : connected")

    ensure_funded(client, wallet.pubkey(), min_sol=0.5)

    balance = get_balance_sol(client, wallet.pubkey())
    if balance < 0.3:
        print(f"\nERROR: Need at least 0.3 SOL.  Current: {balance:.4f}")
        print("  Run:  solana airdrop 2 --url devnet")
        sys.exit(1)
    print(f"Balance    : {balance:.4f} SOL  (ready)\n")

    if args.scenario in ("success", "all"):
        run_success_scenario(client, wallet)
    if args.scenario in ("refund", "all"):
        run_refund_scenario(client, wallet)

    print_summary()
    print("\nDone.")

if __name__ == "__main__":
    main()
