"""
Solana Crowdfunding Platform - Python Test Client
Repo: https://github.com/CalvinSkunnies/Solana-Crowdfunding

WHAT THIS FIXES
────────────────────────────────────────────────────────────────
The contribution TX was failing silently because the on-chain Contribute
instruction expects the vault PDA to be passed as an explicit account
(4 accounts total: donor, campaign, vault, system_program) — but the
previous client only passed 3 (donor, campaign, system_program).

The on-chain program uses a pattern like:
    accounts[0] = donor          (signer, writable)
    accounts[1] = campaign       (writable)
    accounts[2] = vault_pda      (writable)   ← was missing
    accounts[3] = system_program

This version passes the vault PDA explicitly so the program can verify
it and execute the CPI transfer into it.

The Withdraw instruction similarly needs the vault PDA:
    accounts[0] = creator        (signer, writable)
    accounts[1] = campaign       (writable)
    accounts[2] = vault_pda      (writable)   ← was missing

CHECKLIST
────────────────────────────────────────────────────────────────
1. Create campaign   goal=0.1 SOL, deadline=45s (or 24h with --long-deadline)
2. Contribute 0.06 SOL  →  raised = 0.06 SOL
3. Contribute 0.05 SOL  →  raised = 0.11 SOL  (exceeds goal)
4. Withdraw BEFORE deadline  →  must FAIL
5. Wait for deadline, withdraw  →  must SUCCEED
6. Withdraw again  →  must FAIL  (already claimed)

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

PROGRAM_ID       = Pubkey.from_string("3Dc6ZJsWiQm6CmDUt5MY4izbdLgpBU2KbhfSmqpVcayM")
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
            sig_obj = resp.value   # already a Signature object
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
# PDA vault derivation  — seeds: [b"vault", campaign_pubkey_bytes]
# =============================================================================

def derive_vault_pda(campaign_pubkey: Pubkey) -> tuple:
    return Pubkey.find_program_address(
        [b"vault", bytes(campaign_pubkey)],
        PROGRAM_ID,
    )

# =============================================================================
# Instruction builders
#
# IMPORTANT ACCOUNT LAYOUTS — must match src/lib.rs exactly:
#
# CreateCampaign (0x00):
#   [0] creator        signer, writable
#   [1] campaign       signer, writable   (new account being created)
#   [2] system_program
#   [3] rent_sysvar
#
# Contribute (0x01):
#   [0] donor          signer, writable
#   [1] campaign       writable           (state account — tracks raised)
#   [2] vault_pda      writable           (receives the lamports)
#   [3] system_program
#
# Withdraw (0x02):
#   [0] creator        signer, writable
#   [1] campaign       writable
#   [2] vault_pda      writable           (sends lamports back to creator)
#
# Refund (0x03):
#   [0] donor          signer, writable
#   [1] campaign       writable
#   [2] vault_pda      writable
# =============================================================================

def ix_create_campaign(creator: Pubkey, campaign: Pubkey,
                        goal: int, deadline: int) -> Instruction:
    """[0x00] | goal u64 LE | deadline i64 LE"""
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

def ix_contribute(donor: Pubkey, campaign: Pubkey,
                   vault: Pubkey, amount: int) -> Instruction:
    """
    [0x01] | amount u64 LE
    vault_pda is passed explicitly so the program can CPI-transfer
    lamports into it and verify it matches [b"vault", campaign].
    """
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

def ix_withdraw(creator: Pubkey, campaign: Pubkey,
                 vault: Pubkey) -> Instruction:
    """
    [0x02]
    vault_pda is passed so the program can sign for it with invoke_signed
    and transfer its balance to the creator.
    """
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=creator,  is_signer=True,  is_writable=True),
            AccountMeta(pubkey=campaign, is_signer=False, is_writable=True),
            AccountMeta(pubkey=vault,    is_signer=False, is_writable=True),
        ],
        data=bytes([2]),
    )

def ix_refund(donor: Pubkey, campaign: Pubkey,
               vault: Pubkey, amount: int) -> Instruction:
    """[0x03] | amount u64 LE"""
    data = bytes([3]) + struct.pack("<Q", amount)
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=donor,    is_signer=True,  is_writable=True),
            AccountMeta(pubkey=campaign, is_signer=False, is_writable=True),
            AccountMeta(pubkey=vault,    is_signer=False, is_writable=True),
        ],
        data=data,
    )

# =============================================================================
# Transaction helpers
# =============================================================================

def _extract_logs(err_str: str) -> list:
    """Pull 'Program log: ...' lines out of an exception string."""
    try:
        return re.findall(r'Program log: (.*?)(?:\\n|")', err_str)
    except Exception:
        return []

def send_tx(client: Client, instructions: list, signers: list,
            label: str = "tx") -> Optional[Signature]:
    """
    Build + sign + send a VersionedTransaction.
    Returns a Signature object on success, None on failure.
    Prints program logs on failure so the real error is always visible.
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
        sig  = resp.value   # Signature object — do NOT stringify
        print(f"  [{label}] Sig     : {sig}")
        print(f"  [{label}] Explorer: https://explorer.solana.com/tx/{sig}?cluster=devnet")
        return sig
    except Exception as e:
        err_str = str(e)
        print(f"  [{label}] FAILED  : {err_str[:500]}")
        logs = _extract_logs(err_str)
        if logs:
            print(f"  [{label}] Program logs:")
            for line in logs:
                print(f"    >> {line}")
        else:
            # If preflight didn't give us logs, print the full error so
            # we can diagnose the real account layout mismatch.
            print(f"  [{label}] Full error: {err_str}")
        return None

def wait_confirm(client: Client, sig: Optional[Signature],
                 timeout: int = 60) -> bool:
    """
    Poll for confirmation. sig must be a Signature object (not a str).
    Uses str(cs).lower() to safely compare the solders enum variant.
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
    """Fetch full TX record — use after wait_confirm times out."""
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
            print("  Not found on-chain — may still be processing.")
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
    vault_pubkey, vault_bump = derive_vault_pda(campaign_pubkey)

    print(f"\n  Creator   : {creator_pubkey}")
    print(f"  Campaign  : {campaign_pubkey}")
    print(f"  Vault PDA : {vault_pubkey}  (bump={vault_bump})")
    print(f"  Goal      : {GOAL_SOL} SOL  ({goal_lamps:,} lamports)")
    print(f"  Contrib 1 : {CONTRIB_1_SOL} SOL")
    print(f"  Contrib 2 : {CONTRIB_2_SOL} SOL")
    print(f"  Deadline  : {deadline_desc}")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 1 — Create campaign
    # ─────────────────────────────────────────────────────────────────────────
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

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 2 — Contribute 0.06 SOL
    # ─────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 66)
    print(f"STEP 2 — Contribute {CONTRIB_1_SOL} SOL  →  raised = {CONTRIB_1_SOL} SOL")
    print("─" * 66)

    vault_pre_1  = get_balance_sol(client, vault_pubkey)
    creator_pre_1 = get_balance_sol(client, creator_pubkey)

    ok = send_and_confirm(
        client,
        [ix_contribute(creator_pubkey, campaign_pubkey, vault_pubkey, contrib1_lamps)],
        [wallet],
        "contribute_0.06",
    )

    vault_post_1  = get_balance_sol(client, vault_pubkey)
    creator_post_1 = get_balance_sol(client, creator_pubkey)
    vault_delta_1  = vault_post_1 - vault_pre_1
    spent_1        = creator_pre_1 - creator_post_1

    print(f"\n  Vault   : {vault_pre_1:.6f} → {vault_post_1:.6f}  (+{vault_delta_1:.6f} SOL)")
    print(f"  Creator : {creator_pre_1:.6f} → {creator_post_1:.6f}  (-{spent_1:.6f} SOL incl fee)")

    vault_ok_1 = vault_delta_1 > 0
    record(2, f"Contribute {CONTRIB_1_SOL} SOL → raised={CONTRIB_1_SOL} SOL",
           ok and vault_ok_1,
           "" if (ok and vault_ok_1) else
           ("TX failed" if not ok else "Vault balance unchanged — vault PDA mismatch?"))

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 3 — Contribute 0.05 SOL  →  raised = 0.11 SOL
    # ─────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 66)
    print(f"STEP 3 — Contribute {CONTRIB_2_SOL} SOL  →  raised = "
          f"{round(CONTRIB_1_SOL + CONTRIB_2_SOL, 2)} SOL  (exceeds goal)")
    print("─" * 66)

    vault_pre_2 = get_balance_sol(client, vault_pubkey)

    ok = send_and_confirm(
        client,
        [ix_contribute(creator_pubkey, campaign_pubkey, vault_pubkey, contrib2_lamps)],
        [wallet],
        "contribute_0.05",
    )

    vault_post_2  = get_balance_sol(client, vault_pubkey)
    vault_delta_2 = vault_post_2 - vault_pre_2

    print(f"\n  Vault   : {vault_pre_2:.6f} → {vault_post_2:.6f}  (+{vault_delta_2:.6f} SOL)")

    vault_ok_2 = vault_delta_2 > 0
    record(3, f"Contribute {CONTRIB_2_SOL} SOL → raised="
               f"{round(CONTRIB_1_SOL + CONTRIB_2_SOL, 2)} SOL",
           ok and vault_ok_2,
           "" if (ok and vault_ok_2) else
           ("TX failed" if not ok else "Vault balance unchanged"))

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 4 — Withdraw BEFORE deadline → must FAIL
    # ─────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 66)
    print("STEP 4 — Withdraw BEFORE deadline  →  must FAIL")
    print("─" * 66)
    secs = max(0, deadline - int(time.time()))
    print(f"  {secs}s until deadline")

    sig_early = send_tx(
        client,
        [ix_withdraw(creator_pubkey, campaign_pubkey, vault_pubkey)],
        [wallet],
        "early_withdraw",
    )

    if sig_early is None:
        record(4, "Withdraw before deadline → rejected ✓", True,
               "Rejected at preflight")
    else:
        early_ok = wait_confirm(client, sig_early, timeout=20)
        if early_ok:
            record(4, "Withdraw before deadline → rejected ✓", False,
                   "⚠️  Program ALLOWED early withdrawal — deadline check missing!")
        else:
            record(4, "Withdraw before deadline → rejected ✓", True,
                   "Rejected by validator")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 5 — Wait for deadline, then withdraw → must SUCCEED
    # ─────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 66)
    print("STEP 5 — Wait for deadline, then withdraw  →  must SUCCEED")
    print("─" * 66)

    remaining = deadline - int(time.time())
    if remaining > 0:
        wait_secs = remaining + 3
        print(f"  Waiting {wait_secs}s...")
        for t in range(wait_secs, 0, -1):
            print(f"  ⏳ {t:>3}s remaining...", end="\r")
            time.sleep(1)
        print()
    else:
        print("  Deadline already passed.")

    creator_pre_w = get_balance_sol(client, creator_pubkey)

    ok = send_and_confirm(
        client,
        [ix_withdraw(creator_pubkey, campaign_pubkey, vault_pubkey)],
        [wallet],
        "withdraw",
    )

    creator_post_w = get_balance_sol(client, creator_pubkey)
    net_w          = creator_post_w - creator_pre_w
    print(f"  Creator balance change : {net_w:+.6f} SOL")

    record(5, "Withdraw after deadline → success", ok,
           "" if ok else "Withdraw TX failed — check program logs above")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 6 — Withdraw again → must FAIL (AlreadyClaimed)
    # ─────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 66)
    print("STEP 6 — Withdraw again  →  must FAIL  (AlreadyClaimed)")
    print("─" * 66)

    sig_double = send_tx(
        client,
        [ix_withdraw(creator_pubkey, campaign_pubkey, vault_pubkey)],
        [wallet],
        "double_withdraw",
    )

    if sig_double is None:
        record(6, "Double withdraw → rejected ✓  (claimed=true)", True,
               "Rejected at preflight")
    else:
        double_ok = wait_confirm(client, sig_double, timeout=20)
        if double_ok:
            record(6, "Double withdraw → rejected ✓  (claimed=true)", False,
                   "⚠️  Program ALLOWED double withdrawal — claimed flag never set!")
        else:
            record(6, "Double withdraw → rejected ✓  (claimed=true)", True,
                   "Rejected by validator")

    print_summary()

# =============================================================================
# CLI
# =============================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="Solana Crowdfunding — Devnet test checklist")
    p.add_argument("--keypair",       metavar="PATH")
    p.add_argument("--new-wallet",    action="store_true")
    p.add_argument("--long-deadline", action="store_true",
                   help="Use 24h deadline instead of 45s")
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
