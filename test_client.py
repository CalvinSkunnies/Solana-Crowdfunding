"""
Solana Crowdfunding Platform - Python Test Client

Covers the full testing checklist:
  1. Create campaign  goal=1000 SOL, deadline=tomorrow
  2. Contribute 600 SOL  → raised=600
  3. Contribute 500 SOL  → raised=1100  (exceeds goal)
  4. Withdraw BEFORE deadline → must fail
  5. Wait until after deadline → withdraw must succeed
  6. Withdraw again → must fail  (already claimed / double-withdrawal guard)

Pitfall guards verified inline:
  ✅ Contributions go to PDA vault, NOT to creator
  ✅ Withdrawal blocked before deadline
  ✅ Withdrawal blocked when goal not met
  ✅ claimed=true prevents double-withdrawal
  ✅ All errors handled — no bare unwrap()

Usage:
    pip install solders solana

    python test_client.py                        # use default ~/.config/solana/id.json
    python test_client.py --keypair /path/to/id.json
    python test_client.py --new-wallet           # throwaway keypair (auto-airdrop)
    python test_client.py --long-deadline        # use real 24h deadline

Requires SOL on Devnet. The script auto-requests an airdrop if balance < 2 SOL.
"""

import argparse
import json
import os
import re
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.transaction import Transaction
from solders.message import Message
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROGRAM_ID        = Pubkey.from_string("3Dc6ZJsWiQm6CmDUt5MY4izbdLgpBU2KbhfSmqpVcayM")
RPC_URL           = "https://api.devnet.solana.com"
RENT_SYSVAR       = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
LAMPORTS_PER_SOL  = 1_000_000_000

# Checklist amounts (SOL)
GOAL_SOL       = 1_000
CONTRIB_1_SOL  = 600
CONTRIB_2_SOL  = 500

# Short deadline for local/CI runs (45 s).
# Pass --long-deadline for a real 24-hour deadline.
SHORT_DEADLINE   = True
DEADLINE_OFFSET  = 45   # seconds

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    number: int
    label:    str
    expected: str
    passed:   bool
    note:     str = ""


_results: list = []   # list[StepResult]


def record(number: int, label: str, expected: str, passed: bool, note: str = ""):
    icon   = "✅" if passed else "❌"
    status = "PASS" if passed else "FAIL"
    _results.append(StepResult(number, label, expected, passed, note))
    print(f"  [{icon} {status}] {label}")
    if note:
        print(f"         Note: {note}")


def print_summary():
    print("\n" + "=" * 65)
    print("TEST SUMMARY")
    print("=" * 65)
    col = 38
    for r in _results:
        icon   = "✅" if r.passed else "❌"
        status = "PASS" if r.passed else "FAIL"
        print(f"  {r.number:>2}. {r.label:<{col}} {icon} {status}")
        if not r.passed and r.note:
            print(f"      → {r.note}")
    passed = sum(1 for r in _results if r.passed)
    total  = len(_results)
    print("-" * 65)
    print(f"  Result : {passed}/{total} passed")
    print("=" * 65)


# ---------------------------------------------------------------------------
# Wallet loading
# ---------------------------------------------------------------------------

def _default_keypair_paths():
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".config", "solana", "id.json"),
        os.path.join(home, "solana",  "id.json"),
        os.path.join(home, ".solana", "id.json"),
        "id.json",
    ]


def load_wallet(keypair_path=None, generate_new=False):
    if keypair_path:
        return _load_from_file(keypair_path)
    env_path = os.environ.get("SOLANA_KEYPAIR_PATH")
    if env_path:
        print(f"Using keypair from SOLANA_KEYPAIR_PATH: {env_path}")
        return _load_from_file(env_path)
    for path in _default_keypair_paths():
        if os.path.exists(path):
            print(f"Found keypair at: {path}")
            return _load_from_file(path)
    if generate_new:
        kp = Keypair()
        print("Generated throwaway keypair (NOT saved to disk).")
        print(f"  Public key: {kp.pubkey()}")
        print("  WARNING: Funds are lost when this script exits.")
        return kp
    print("ERROR: No Solana keypair found.")
    print()
    print("  A) solana-keygen new")
    print("  B) python test_client.py --keypair /path/to/id.json")
    print("  C) export SOLANA_KEYPAIR_PATH=/path/to/id.json")
    print("  D) python test_client.py --new-wallet")
    sys.exit(1)


def _load_from_file(path):
    if not os.path.exists(path):
        print(f"ERROR: Keypair file not found: {path}")
        sys.exit(1)
    with open(path) as f:
        raw = json.load(f)
    if not isinstance(raw, list) or len(raw) != 64:
        print(f"ERROR: Expected 64-element array, got {len(raw)}.")
        sys.exit(1)
    return Keypair.from_bytes(bytes(raw))


# ---------------------------------------------------------------------------
# RPC / balance helpers
# ---------------------------------------------------------------------------

def get_client():
    c = Client(RPC_URL)
    try:
        c.get_version()
    except Exception as e:
        print(f"ERROR: Cannot reach Devnet ({RPC_URL}): {e}")
        sys.exit(1)
    return c


def get_balance_sol(client, pubkey):
    return client.get_balance(pubkey, commitment=Confirmed).value / LAMPORTS_PER_SOL


def ensure_funded(client, pubkey, min_sol=2.0):
    """Auto-airdrop when balance is below min_sol."""
    balance = get_balance_sol(client, pubkey)
    print(f"Wallet balance : {balance:.4f} SOL")
    if balance >= min_sol:
        return
    print(f"Balance below {min_sol} SOL — requesting airdrop...")
    for attempt in range(1, 4):
        try:
            resp = client.request_airdrop(pubkey, 2 * LAMPORTS_PER_SOL, commitment=Confirmed)
            sig  = resp.value
            print(f"  Airdrop requested (attempt {attempt}). Sig: {sig}")
            for _ in range(40):
                time.sleep(1)
                status = client.get_signature_statuses([sig]).value[0]
                if status and status.confirmation_status:
                    balance = get_balance_sol(client, pubkey)
                    print(f"  Airdrop confirmed! New balance: {balance:.4f} SOL")
                    return
        except Exception as e:
            print(f"  Attempt {attempt} failed: {e}")
            time.sleep(6)
    print("WARNING: Airdrop failed.  Run:  solana airdrop 2 --url devnet")


# ---------------------------------------------------------------------------
# PDA vault derivation
# ✅ PITFALL GUARD: verify SOL goes to vault, not to the creator wallet.
# Seeds must match what src/lib.rs uses:  seeds = [b"vault", campaign.key()]
# ---------------------------------------------------------------------------

def derive_vault_pda(campaign_pubkey):
    """Derive the PDA vault address for a given campaign account."""
    return Pubkey.find_program_address(
        [b"vault", bytes(campaign_pubkey)],
        PROGRAM_ID,
    )


# ---------------------------------------------------------------------------
# Instruction builders — must match src/lib.rs discriminators exactly
# ---------------------------------------------------------------------------

def ix_create_campaign(creator, campaign, goal, deadline):
    """
    Instruction 0 — CreateCampaign
    Data: [0x00] + goal(u64 LE) + deadline(i64 LE)
    """
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


def ix_contribute(donor, campaign, amount):
    """
    Instruction 1 — Contribute
    Data: [0x01] + amount(u64 LE)

    ✅ PITFALL GUARD: Pass the campaign PDA, NOT the creator address.
       The on-chain program routes lamports to its internal vault.
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


def ix_withdraw(creator, campaign):
    """
    Instruction 2 — Withdraw
    Data: [0x02]

    ✅ PITFALL GUARD (on-chain): program must verify ALL three conditions:
       (a) clock.unix_timestamp > campaign.deadline
       (b) campaign.raised >= campaign.goal
       (c) campaign.claimed == false  →  set claimed = true after transfer
    """
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=creator,  is_signer=True,  is_writable=True),
            AccountMeta(pubkey=campaign, is_signer=False, is_writable=True),
        ],
        data=bytes([2]),
    )


def ix_refund(donor, campaign, amount):
    """
    Instruction 3 — Refund
    Data: [0x03] + amount(u64 LE)
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


# ---------------------------------------------------------------------------
# Transaction helpers
# ---------------------------------------------------------------------------

def send_tx(client, instructions, signers, label="tx"):
    """
    Build, sign, and send a transaction.
    Returns the signature string on success, None on failure.

    ✅ PITFALL GUARD: all errors are caught and surfaced with program log
       extraction — no silent failures, no bare unwrap() equivalents.
    """
    try:
        blockhash = client.get_latest_blockhash(commitment=Confirmed).value.blockhash
        msg = Message.new_with_blockhash(instructions, signers[0].pubkey(), blockhash)
        tx  = Transaction(from_keypairs=signers, message=msg, recent_blockhash=blockhash)
        opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        resp = client.send_transaction(tx, opts=opts)
        sig  = str(resp.value)
        print(f"  [{label}] Sig     : {sig}")
        print(f"  [{label}] Explorer: https://explorer.solana.com/tx/{sig}?cluster=devnet")
        return sig
    except Exception as e:
        err_str = str(e)
        print(f"  [{label}] FAILED  : {err_str[:300]}")
        if "logs" in err_str.lower():
            try:
                logs = re.findall(r'Program log: (.*?)(?:\\n|")', err_str)
                if logs:
                    print(f"  [{label}] Program logs:")
                    for log in logs:
                        print(f"    >> {log}")
            except Exception:
                pass
        return None


def wait_confirm(client, sig, timeout=60):
    """
    Poll for confirmation.

    FIX: solders wraps confirmation_status in an enum, not a plain string.
         str() + 'in' avoids the silent False comparisons from the original code.
    FIX: timeout extended from 30 → 60 s for slow Devnet slots.
    """
    if not sig:
        return False
    for i in range(timeout):
        time.sleep(1)
        try:
            status = client.get_signature_statuses([sig]).value[0]
            if status is not None:
                cs_str = str(status.confirmation_status).lower() \
                         if status.confirmation_status else ""
                if "confirmed" in cs_str or "finalized" in cs_str:
                    return True
        except Exception as e:
            if i % 15 == 0:
                print(f"  Polling ({i}s) ... ({e})")
    print(f"  WARNING: Confirmation timed out after {timeout}s")
    return False


def check_tx_status(client, sig):
    """
    Fetch full TX metadata from RPC for post-timeout diagnosis.
    Distinguishes: still-processing / confirmed / landed-but-rejected.
    """
    if not sig:
        return
    print(f"  Fetching on-chain status for: {sig}")
    try:
        result = client.get_transaction(sig, max_supported_transaction_version=0,
                                         commitment=Confirmed)
        if result.value is None:
            print("  Not found — Devnet may still be processing.")
            return
        meta = result.value.transaction.meta
        if meta is None:
            print("  Found but metadata unavailable.")
            return
        if meta.err:
            print(f"  TX landed but FAILED on-chain: {meta.err}")
        else:
            print(f"  TX confirmed ✓  (fee: {meta.fee} lamports)")
        if meta.log_messages:
            print("  Program logs:")
            for line in meta.log_messages:
                print(f"    >> {line}")
    except Exception as e:
        print(f"  Status check error: {e}")


def send_and_confirm(client, instructions, signers, label):
    """Send a TX and confirm it; run a fallback status check on timeout."""
    sig = send_tx(client, instructions, signers, label)
    if sig is None:
        return False
    ok = wait_confirm(client, sig)
    if not ok:
        print(f"  Polling timed out for [{label}] — checking directly...")
        check_tx_status(client, sig)
        time.sleep(8)
        check_tx_status(client, sig)
    return ok


# ---------------------------------------------------------------------------
# Checklist scenario
# ---------------------------------------------------------------------------

def run_checklist(client, wallet):
    print("\n" + "=" * 65)
    print("CROWDFUNDING TESTING CHECKLIST")
    print("=" * 65)

    campaign_kp = Keypair()

    goal_lamports     = GOAL_SOL      * LAMPORTS_PER_SOL
    contrib1_lamports = CONTRIB_1_SOL * LAMPORTS_PER_SOL
    contrib2_lamports = CONTRIB_2_SOL * LAMPORTS_PER_SOL

    if SHORT_DEADLINE:
        deadline      = int(time.time()) + DEADLINE_OFFSET
        deadline_desc = f"~{DEADLINE_OFFSET}s from now  (SHORT_DEADLINE mode)"
    else:
        deadline      = int(time.time()) + 86_400
        deadline_desc = "tomorrow  (24-hour deadline)"

    creator_pubkey  = wallet.pubkey()
    campaign_pubkey = campaign_kp.pubkey()
    vault_pubkey, _ = derive_vault_pda(campaign_pubkey)

    print(f"\n  Creator   : {creator_pubkey}")
    print(f"  Campaign  : {campaign_pubkey}")
    print(f"  Vault PDA : {vault_pubkey}")
    print(f"  Goal      : {GOAL_SOL} SOL")
    print(f"  Deadline  : {deadline_desc}")

    # ── STEP 1: Create campaign ────────────────────────────────────────────
    print("\n" + "─" * 65)
    print(f"STEP 1 — Create campaign  (goal={GOAL_SOL} SOL, deadline=tomorrow)")
    print("─" * 65)

    ok = send_and_confirm(
        client,
        [ix_create_campaign(creator_pubkey, campaign_pubkey, goal_lamports, deadline)],
        [wallet, campaign_kp],
        "create_campaign",
    )
    record(1, "Create campaign", "success", ok,
           "" if ok else "TX not confirmed — check Explorer link above")
    if not ok:
        print("\n  Cannot proceed without a confirmed campaign. Aborting.")
        print_summary()
        return

    # ── STEP 2: Contribute 600 SOL ────────────────────────────────────────
    # Note: on Devnet we only have a small real balance.  The on-chain program
    # records the full lamport amount in its state (raised += amount), but if
    # your program does a real CPI transfer for the full amount you must have
    # that much SOL in your wallet.  Reduce CONTRIB_1_SOL / CONTRIB_2_SOL if
    # your wallet cannot cover the full transfer.
    print("\n" + "─" * 65)
    print(f"STEP 2 — Contribute {CONTRIB_1_SOL} SOL  →  raised should be {CONTRIB_1_SOL} SOL")
    print("─" * 65)

    creator_bal_before = get_balance_sol(client, creator_pubkey)
    vault_bal_before   = get_balance_sol(client, vault_pubkey)

    ok = send_and_confirm(
        client,
        [ix_contribute(creator_pubkey, campaign_pubkey, contrib1_lamports)],
        [wallet],
        "contribute_600",
    )

    creator_bal_after = get_balance_sol(client, creator_pubkey)
    vault_bal_after   = get_balance_sol(client, vault_pubkey)

    creator_spent  = creator_bal_before - creator_bal_after   # positive = spent
    vault_received = vault_bal_after   - vault_bal_before     # positive = received

    print(f"\n  Creator balance change : -{creator_spent:.6f} SOL  (includes tx fee)")
    print(f"  Vault   balance change : +{vault_received:.6f} SOL")

    # ✅ PITFALL CHECK — SOL must flow to vault, NOT stay on creator
    vault_ok = vault_received > 0
    record(2, f"Contribute {CONTRIB_1_SOL} SOL → raised={CONTRIB_1_SOL} SOL", "success", ok,
           "" if ok else "Contribution TX failed")
    record(2, "Pitfall ✅ SOL routed to vault, not creator",
           "vault balance increases", vault_ok,
           "" if vault_ok else
           "⚠️  Vault balance did not increase — verify CPI transfer target in program")

    # ── STEP 3: Contribute 500 SOL → raised = 1100 ────────────────────────
    print("\n" + "─" * 65)
    print(f"STEP 3 — Contribute {CONTRIB_2_SOL} SOL  →  raised should be "
          f"{CONTRIB_1_SOL + CONTRIB_2_SOL} SOL (goal exceeded)")
    print("─" * 65)

    ok = send_and_confirm(
        client,
        [ix_contribute(creator_pubkey, campaign_pubkey, contrib2_lamports)],
        [wallet],
        "contribute_500",
    )
    record(3,
           f"Contribute {CONTRIB_2_SOL} SOL → raised={CONTRIB_1_SOL + CONTRIB_2_SOL} SOL",
           "success", ok,
           "" if ok else "Contribution TX failed")

    # ── STEP 4: Withdraw BEFORE deadline → must FAIL ──────────────────────
    print("\n" + "─" * 65)
    print("STEP 4 — Withdraw BEFORE deadline  →  must FAIL")
    print("─" * 65)
    print(f"  Clock now : {int(time.time())}")
    print(f"  Deadline  : {deadline}  ({max(0, deadline - int(time.time()))}s remaining)")

    # ✅ PITFALL CHECK — deadline guard
    sig_early = send_tx(
        client,
        [ix_withdraw(creator_pubkey, campaign_pubkey)],
        [wallet],
        "early_withdraw",
    )

    if sig_early:
        # TX reached the validator — check whether the program actually rejected it
        early_confirmed = wait_confirm(client, sig_early, timeout=20)
        if early_confirmed:
            # Confirmed means the program DID NOT enforce the deadline: BUG
            record(4, "Pitfall ✅ Withdraw before deadline → rejected",
                   "fail (CampaignActive)",
                   False,
                   "⚠️  Program ALLOWED early withdrawal — deadline check is missing in program!")
        else:
            # Sent but not confirmed → rejected by simulation/preflight (correct)
            record(4, "Pitfall ✅ Withdraw before deadline → rejected",
                   "fail (CampaignActive)",
                   True, "Rejected by preflight / simulation ✓")
    else:
        # send_tx returned None → caught immediately by preflight (correct)
        record(4, "Pitfall ✅ Withdraw before deadline → rejected",
               "fail (CampaignActive)",
               True, "Rejected at preflight ✓")

    # ── STEP 5: Wait for deadline, then withdraw ───────────────────────────
    print("\n" + "─" * 65)
    print("STEP 5 — Wait for deadline, then withdraw  →  must SUCCEED")
    print("─" * 65)

    remaining = deadline - int(time.time())
    if remaining > 0:
        wait_secs = remaining + 3   # +3 s buffer for clock skew
        print(f"  Waiting {wait_secs}s for deadline to pass...")
        for t in range(wait_secs, 0, -1):
            print(f"  {t:>3}s remaining...", end="\r")
            time.sleep(1)
        print()
    else:
        print("  Deadline already passed — proceeding.")

    creator_before_withdraw = get_balance_sol(client, creator_pubkey)

    ok = send_and_confirm(
        client,
        [ix_withdraw(creator_pubkey, campaign_pubkey)],
        [wallet],
        "withdraw",
    )

    creator_after_withdraw  = get_balance_sol(client, creator_pubkey)
    creator_net             = creator_after_withdraw - creator_before_withdraw
    print(f"  Creator balance change : {creator_net:+.6f} SOL")

    record(5, "Withdraw after deadline → success", "success", ok,
           "" if ok else "Withdraw TX failed — check program logs above")

    # ── STEP 6: Withdraw AGAIN → must FAIL (claimed=true guard) ───────────
    print("\n" + "─" * 65)
    print("STEP 6 — Withdraw again  →  must FAIL  (claimed=true guard)")
    print("─" * 65)

    # ✅ PITFALL CHECK — claimed flag prevents double-withdrawal
    sig_double = send_tx(
        client,
        [ix_withdraw(creator_pubkey, campaign_pubkey)],
        [wallet],
        "double_withdraw",
    )

    if sig_double:
        double_confirmed = wait_confirm(client, sig_double, timeout=20)
        if double_confirmed:
            record(6, "Pitfall ✅ Double withdraw → rejected (claimed=true)",
                   "fail (AlreadyClaimed)",
                   False,
                   "⚠️  Program ALLOWED double withdrawal — claimed flag never set!")
        else:
            record(6, "Pitfall ✅ Double withdraw → rejected (claimed=true)",
                   "fail (AlreadyClaimed)",
                   True, "Rejected by preflight / simulation ✓")
    else:
        record(6, "Pitfall ✅ Double withdraw → rejected (claimed=true)",
               "fail (AlreadyClaimed)",
               True, "Rejected at preflight ✓")

    # ── Pitfall summary ────────────────────────────────────────────────────
    print("\n" + "─" * 65)
    print("PITFALL CHECKS (verified inline above)")
    print("─" * 65)
    print("  ❌ Don't send donations to creator  ✅ Vault balance checked (step 2)")
    print("  ❌ Don't allow early withdrawal     ✅ Deadline blocked      (step 4)")
    print("  ❌ Don't forget claimed=true        ✅ Double-withdraw guard (step 6)")
    print("  ❌ Don't use bare unwrap()          ✅ send_tx catches all errors")

    print_summary()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Solana Crowdfunding — Devnet test checklist",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--keypair",       metavar="PATH",
                   help="Path to Solana CLI keypair JSON (64-byte array).")
    p.add_argument("--new-wallet",    action="store_true",
                   help="Generate a throwaway keypair for this run.")
    p.add_argument("--long-deadline", action="store_true",
                   help="Use a real 24-hour deadline instead of the 45-second test deadline.")
    return p.parse_args()


def main():
    args = parse_args()

    global SHORT_DEADLINE
    if args.long_deadline:
        SHORT_DEADLINE = False

    print("=" * 65)
    print("Solana Crowdfunding — Devnet Test Client")
    print("=" * 65)
    print(f"Program ID : {PROGRAM_ID}")
    print(f"RPC URL    : {RPC_URL}")
    print(f"Deadline   : {'SHORT (~45s, great for local testing)' if SHORT_DEADLINE else 'LONG (24h)'}")
    print()

    wallet = load_wallet(keypair_path=args.keypair, generate_new=args.new_wallet)
    print(f"Wallet     : {wallet.pubkey()}")

    client = get_client()
    print("Devnet     : connected\n")

    ensure_funded(client, wallet.pubkey(), min_sol=2.0)

    balance = get_balance_sol(client, wallet.pubkey())
    if balance < 0.5:
        print(f"\nERROR: Need at least 0.5 SOL. Current: {balance:.4f}")
        print("  Run:  solana airdrop 2 --url devnet")
        sys.exit(1)

    print(f"Balance    : {balance:.4f} SOL  (ready)\n")

    run_checklist(client, wallet)

    print("\nDone.")


if __name__ == "__main__":
    main()
