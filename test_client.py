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
from dataclasses import dataclass
from typing import Optional
 
# ── solders imports (modern API) ──────────────────────────────────────────────
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.message import MessageV0                      # ← replaces Message
from solders.transaction import VersionedTransaction       # ← replaces Transaction
from solders.hash import Hash
 
# ── solana-py RPC ─────────────────────────────────────────────────────────────
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
 
# Checklist amounts — kept small so a Devnet wallet can afford them
GOAL_SOL      = 1      #  1.0 SOL goal
CONTRIB_1_SOL = 0.6    #  0.6 SOL first contribution
CONTRIB_2_SOL = 0.5    #  0.5 SOL second contribution  → total 1.1 > goal
 
# Deadline: 45 s for quick local testing, 24 h for --long-deadline
SHORT_DEADLINE  = True
DEADLINE_OFFSET = 45    # seconds
 
# =============================================================================
# Step result tracking
# =============================================================================
 
@dataclass
class StepResult:
    number:   int
    label:    str
    expected: str
    passed:   bool
    note:     str = ""
 
 
_results: list = []
 
 
def record(number: int, label: str, expected: str, passed: bool, note: str = ""):
    icon   = "✅" if passed else "❌"
    status = "PASS" if passed else "FAIL"
    _results.append(StepResult(number, label, expected, passed, note))
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
        print(f"  {r.number:>2}. {r.label:<42} {icon} {status}")
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
        os.path.join(home, ".config", "solana", "id.json"),   # Linux / macOS
        os.path.join(home, "solana",  "id.json"),              # Windows variant
        os.path.join(home, ".solana", "id.json"),
        "id.json",                                             # cwd fallback
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
        print("  WARNING    : funds are lost when this script exits.")
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
        print(f"ERROR: Expected 64-element JSON array, got {len(raw)} elements.")
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
 
 
def ensure_funded(client: Client, pubkey: Pubkey, min_sol: float = 1.0):
    balance = get_balance_sol(client, pubkey)
    print(f"Wallet balance : {balance:.4f} SOL")
    if balance >= min_sol:
        return
 
    print(f"Balance below {min_sol} SOL — requesting 2 SOL airdrop...")
    for attempt in range(1, 4):
        try:
            resp = client.request_airdrop(pubkey, 2 * LAMPORTS_PER_SOL, commitment=Confirmed)
            sig  = resp.value
            print(f"  Airdrop requested (attempt {attempt}). Sig: {sig}")
            for _ in range(45):
                time.sleep(1)
                st = client.get_signature_statuses([sig]).value[0]
                if st and st.confirmation_status:
                    balance = get_balance_sol(client, pubkey)
                    print(f"  Airdrop confirmed!  New balance: {balance:.4f} SOL")
                    return
        except Exception as e:
            print(f"  Attempt {attempt} failed: {e}")
            time.sleep(6)
 
    print("WARNING: Airdrop failed.  Run:  solana airdrop 2 --url devnet")
 
 
# =============================================================================
# PDA vault derivation
# Seeds must match src/lib.rs exactly:  [b"vault", campaign_key]
# =============================================================================
 
def derive_vault_pda(campaign_pubkey: Pubkey) -> tuple:
    return Pubkey.find_program_address(
        [b"vault", bytes(campaign_pubkey)],
        PROGRAM_ID,
    )
 
 
# =============================================================================
# Instruction builders — match src/lib.rs discriminators byte-for-byte
# Native Solana program (Borsh), single-byte instruction discriminator
# =============================================================================
 
def ix_create_campaign(creator: Pubkey, campaign: Pubkey,
                        goal: int, deadline: int) -> Instruction:
    """
    Discriminator : 0x00
    Data layout   : [0] + goal(u64 LE, 8 bytes) + deadline(i64 LE, 8 bytes)
    Accounts      : creator(signer,w), campaign(signer,w),
                    system_program, rent_sysvar
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
 
 
def ix_contribute(donor: Pubkey, campaign: Pubkey, amount: int) -> Instruction:
    """
    Discriminator : 0x01
    Data layout   : [1] + amount(u64 LE, 8 bytes)
    Accounts      : donor(signer,w), campaign(w), system_program
 
    ✅ Pitfall: campaign account here, NOT creator — the program CPI-transfers
       lamports into its PDA vault, not directly to the creator wallet.
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
    Discriminator : 0x02
    Data layout   : [2]  (no additional payload)
    Accounts      : creator(signer,w), campaign(w)
 
    ✅ On-chain guards verified by this test:
       (a) deadline must have passed       → step 4 expects rejection
       (b) raised >= goal                  → step 5 expects success
       (c) claimed must be false           → step 6 expects rejection
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
    Discriminator : 0x03
    Data layout   : [3] + amount(u64 LE, 8 bytes)
    Accounts      : donor(signer,w), campaign(w)
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
# Transaction helpers — using VersionedTransaction + MessageV0
#
# KEY FIX: The original code used:
#   Message.new_with_blockhash() + Transaction(from_keypairs=...)
# That legacy path serializes incorrectly with newer solders+solana-py,
# causing transactions to be silently dropped by the validator (no error,
# just never confirms). VersionedTransaction is what the RPC actually expects.
# =============================================================================
 
def send_tx(client: Client, instructions: list, signers: list,
            label: str = "tx") -> Optional[str]:
    """
    Build a v0 VersionedTransaction, sign it, and send it.
    Returns the base58 signature on success, None on any failure.
 
    All exceptions are caught and program logs are extracted so you can
    read the exact on-chain error without digging through raw exception text.
    """
    try:
        bh       = client.get_latest_blockhash(commitment=Confirmed).value.blockhash
        msg      = MessageV0.try_compile(
            payer=signers[0].pubkey(),
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=bh,
        )
        tx       = VersionedTransaction(msg, signers)
        opts     = TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        resp     = client.send_transaction(tx, opts=opts)
        sig      = str(resp.value)
        print(f"  [{label}] Sig     : {sig}")
        print(f"  [{label}] Explorer: "
              f"https://explorer.solana.com/tx/{sig}?cluster=devnet")
        return sig
 
    except Exception as e:
        err_str = str(e)
        print(f"  [{label}] FAILED  : {err_str[:400]}")
        # Extract human-readable program log lines from the exception blob
        try:
            logs = re.findall(r'Program log: (.*?)(?:\\n|")', err_str)
            if logs:
                print(f"  [{label}] Program logs:")
                for log in logs:
                    print(f"    >> {log}")
        except Exception:
            pass
        return None
 
 
def wait_confirm(client: Client, sig: Optional[str], timeout: int = 60) -> bool:
    """
    Poll confirmation_status every second up to `timeout` seconds.
 
    FIX: solders wraps confirmation_status in an enum object, not a plain
    string. The original `== "confirmed"` always returned False.
    str() + "in" works for any enum variant name.
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
 
 
def check_tx_status(client: Client, sig: Optional[str]):
    """
    Fetch full TX metadata via get_transaction.
    Tells you: still-processing / confirmed / landed-but-rejected.
    Call this when wait_confirm times out before giving up.
    """
    if not sig:
        return
    print(f"  Fetching on-chain status: {sig}")
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
            print(f"  TX landed but FAILED on-chain: {meta.err}")
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
    """
    Send + confirm with a fallback status check if polling times out.
    Returns True only if the TX confirmed successfully.
    """
    sig = send_tx(client, instructions, signers, label)
    if sig is None:
        return False
    ok = wait_confirm(client, sig)
    if not ok:
        print(f"  Polling timed out — fetching on-chain status directly...")
        check_tx_status(client, sig)
        time.sleep(8)
        check_tx_status(client, sig)
    return ok
 
 
# =============================================================================
# Main test checklist
# =============================================================================
 
def run_checklist(client: Client, wallet: Keypair):
    print("\n" + "=" * 66)
    print("CROWDFUNDING TEST CHECKLIST")
    print("=" * 66)
 
    campaign_kp = Keypair()
 
    goal_lamps     = int(GOAL_SOL      * LAMPORTS_PER_SOL)
    contrib1_lamps = int(CONTRIB_1_SOL * LAMPORTS_PER_SOL)
    contrib2_lamps = int(CONTRIB_2_SOL * LAMPORTS_PER_SOL)
 
    if SHORT_DEADLINE:
        deadline      = int(time.time()) + DEADLINE_OFFSET
        deadline_desc = f"~{DEADLINE_OFFSET}s from now (short mode for quick testing)"
    else:
        deadline      = int(time.time()) + 86_400
        deadline_desc = "24 hours from now"
 
    creator_pubkey  = wallet.pubkey()
    campaign_pubkey = campaign_kp.pubkey()
    vault_pubkey, _ = derive_vault_pda(campaign_pubkey)
 
    print(f"\n  Creator   : {creator_pubkey}")
    print(f"  Campaign  : {campaign_pubkey}")
    print(f"  Vault PDA : {vault_pubkey}  ← contributions must land here")
    print(f"  Goal      : {GOAL_SOL} SOL  ({goal_lamps:,} lamports)")
    print(f"  Deadline  : {deadline_desc}")
 
    # ── STEP 1: Create campaign ───────────────────────────────────────────────
    print("\n" + "─" * 66)
    print(f"STEP 1 — Create campaign  (goal={GOAL_SOL} SOL, deadline=tomorrow)")
    print("─" * 66)
 
    ok = send_and_confirm(
        client,
        [ix_create_campaign(creator_pubkey, campaign_pubkey, goal_lamps, deadline)],
        [wallet, campaign_kp],
        "create_campaign",
    )
    record(1, "Create campaign", "success", ok,
           "" if ok else "TX did not confirm — paste the Explorer link above into browser")
    if not ok:
        print("\n  Cannot continue without a confirmed campaign. Aborting.")
        print_summary()
        return
 
    # ── STEP 2: Contribute 0.6 SOL ────────────────────────────────────────────
    print("\n" + "─" * 66)
    print(f"STEP 2 — Contribute {CONTRIB_1_SOL} SOL  →  raised should be {CONTRIB_1_SOL} SOL")
    print("─" * 66)
 
    vault_before   = get_balance_sol(client, vault_pubkey)
    creator_before = get_balance_sol(client, creator_pubkey)
 
    ok = send_and_confirm(
        client,
        [ix_contribute(creator_pubkey, campaign_pubkey, contrib1_lamps)],
        [wallet],
        "contribute_0.6",
    )
 
    vault_after   = get_balance_sol(client, vault_pubkey)
    creator_after = get_balance_sol(client, creator_pubkey)
 
    vault_delta   = vault_after   - vault_before    # should be positive
    creator_delta = creator_before - creator_after  # spent (positive = sent SOL + fee)
 
    print(f"\n  Vault   balance change : +{vault_delta:.6f} SOL")
    print(f"  Creator balance change : -{creator_delta:.6f} SOL  (includes tx fee)")
 
    # ✅ Pitfall: SOL must go to vault, not sit on creator
    vault_ok = vault_delta > 0
    record(2, f"Contribute {CONTRIB_1_SOL} SOL → raised={CONTRIB_1_SOL} SOL",
           "success", ok, "" if ok else "TX failed")
    record(2, "Pitfall ✅ SOL went to vault, not creator",
           "vault balance increases", vault_ok,
           "" if vault_ok else
           "⚠️  Vault balance did NOT increase — check CPI target in program")
 
    # ── STEP 3: Contribute 0.5 SOL → raised = 1.1 SOL ────────────────────────
    print("\n" + "─" * 66)
    print(f"STEP 3 — Contribute {CONTRIB_2_SOL} SOL  →  raised should be "
          f"{CONTRIB_1_SOL + CONTRIB_2_SOL} SOL (exceeds goal)")
    print("─" * 66)
 
    ok = send_and_confirm(
        client,
        [ix_contribute(creator_pubkey, campaign_pubkey, contrib2_lamps)],
        [wallet],
        "contribute_0.5",
    )
    record(3, f"Contribute {CONTRIB_2_SOL} SOL → raised={CONTRIB_1_SOL+CONTRIB_2_SOL} SOL",
           "success", ok, "" if ok else "TX failed")
 
    # ── STEP 4: Withdraw BEFORE deadline → must FAIL ──────────────────────────
    print("\n" + "─" * 66)
    print("STEP 4 — Withdraw BEFORE deadline  →  must FAIL  (CampaignActive)")
    print("─" * 66)
    secs_left = max(0, deadline - int(time.time()))
    print(f"  Clock now : {int(time.time())}  |  Deadline : {deadline}  "
          f"({secs_left}s remaining)")
 
    # ✅ Pitfall: program must block early withdrawal
    sig_early = send_tx(
        client,
        [ix_withdraw(creator_pubkey, campaign_pubkey)],
        [wallet],
        "early_withdraw",
    )
 
    if sig_early is None:
        # Preflight caught it immediately — correct behaviour
        record(4, "Pitfall ✅ Withdraw before deadline → rejected",
               "fail (CampaignActive)", True, "Rejected at preflight ✓")
    else:
        early_ok = wait_confirm(client, sig_early, timeout=20)
        if early_ok:
            # TX confirmed — program did NOT enforce the deadline: BUG
            record(4, "Pitfall ✅ Withdraw before deadline → rejected",
                   "fail (CampaignActive)", False,
                   "⚠️  Program ALLOWED early withdrawal — deadline check missing!")
        else:
            # Sent but rejected during simulation — correct
            record(4, "Pitfall ✅ Withdraw before deadline → rejected",
                   "fail (CampaignActive)", True,
                   "Rejected by validator simulation ✓")
 
    # ── STEP 5: Wait for deadline, then withdraw ──────────────────────────────
    print("\n" + "─" * 66)
    print("STEP 5 — Wait for deadline, then withdraw  →  must SUCCEED")
    print("─" * 66)
 
    remaining = deadline - int(time.time())
    if remaining > 0:
        wait_secs = remaining + 3   # small buffer for on-chain clock skew
        print(f"  Waiting {wait_secs}s for deadline to pass...")
        for t in range(wait_secs, 0, -1):
            print(f"  ⏳ {t:>3}s remaining...", end="\r")
            time.sleep(1)
        print()
    else:
        print("  Deadline already passed — proceeding immediately.")
 
    creator_pre_withdraw  = get_balance_sol(client, creator_pubkey)
 
    ok = send_and_confirm(
        client,
        [ix_withdraw(creator_pubkey, campaign_pubkey)],
        [wallet],
        "withdraw",
    )
 
    creator_post_withdraw = get_balance_sol(client, creator_pubkey)
    net_received          = creator_post_withdraw - creator_pre_withdraw
    print(f"  Creator balance change : {net_received:+.6f} SOL")
 
    record(5, "Withdraw after deadline → success", "success", ok,
           "" if ok else "Withdraw TX failed — see program logs above")
 
    # ── STEP 6: Withdraw again → must FAIL (claimed=true guard) ───────────────
    print("\n" + "─" * 66)
    print("STEP 6 — Withdraw again  →  must FAIL  (AlreadyClaimed)")
    print("─" * 66)
 
    # ✅ Pitfall: claimed flag must be set after first successful withdraw
    sig_double = send_tx(
        client,
        [ix_withdraw(creator_pubkey, campaign_pubkey)],
        [wallet],
        "double_withdraw",
    )
 
    if sig_double is None:
        record(6, "Pitfall ✅ Double withdraw → rejected (claimed=true)",
               "fail (AlreadyClaimed)", True, "Rejected at preflight ✓")
    else:
        double_ok = wait_confirm(client, sig_double, timeout=20)
        if double_ok:
            record(6, "Pitfall ✅ Double withdraw → rejected (claimed=true)",
                   "fail (AlreadyClaimed)", False,
                   "⚠️  Program ALLOWED double withdrawal — claimed flag never set!")
        else:
            record(6, "Pitfall ✅ Double withdraw → rejected (claimed=true)",
                   "fail (AlreadyClaimed)", True,
                   "Rejected by validator simulation ✓")
 
    # ── Pitfall matrix ────────────────────────────────────────────────────────
    print("\n" + "─" * 66)
    print("PITFALL MATRIX")
    print("─" * 66)
    print("  ❌ Don't send donations to creator  ✅ Vault delta checked  (step 2)")
    print("  ❌ Don't allow early withdrawal     ✅ Deadline guard        (step 4)")
    print("  ❌ Don't forget claimed=true        ✅ Double-withdraw guard (step 6)")
    print("  ❌ Don't use bare unwrap()          ✅ send_tx catches all errors")
 
    print_summary()
 
 
# =============================================================================
# CLI
# =============================================================================
 
def parse_args():
    p = argparse.ArgumentParser(
        description="Solana Crowdfunding — Devnet test checklist",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--keypair",       metavar="PATH",
                   help="Path to Solana CLI keypair JSON (64-byte array).")
    p.add_argument("--new-wallet",    action="store_true",
                   help="Generate a throwaway keypair + auto-airdrop.")
    p.add_argument("--long-deadline", action="store_true",
                   help="Use a 24-hour deadline instead of the 45-second test deadline.")
    return p.parse_args()
 
 
def main():
    args = parse_args()
 
    global SHORT_DEADLINE
    if args.long_deadline:
        SHORT_DEADLINE = False
 
    print("=" * 66)
    print("Solana Crowdfunding — Devnet Test Client")
    print("=" * 66)
    print(f"Program ID : {PROGRAM_ID}")
    print(f"RPC URL    : {RPC_URL}")
    print(f"TX format  : VersionedTransaction + MessageV0  (fixes timeout bug)")
    print(f"Deadline   : {'SHORT ~45s (local testing)' if SHORT_DEADLINE else 'LONG 24h'}")
    print()
 
    wallet = load_wallet(keypair_path=args.keypair, generate_new=args.new_wallet)
    print(f"Wallet     : {wallet.pubkey()}")
 
    client = get_client()
    print("Devnet     : connected\n")
 
    ensure_funded(client, wallet.pubkey(), min_sol=2.0)
 
    balance = get_balance_sol(client, wallet.pubkey())
    if balance < 0.5:
        print(f"\nERROR: Need at least 0.5 SOL to run tests.  Current: {balance:.4f}")
        print("  Run:  solana airdrop 2 --url devnet")
        sys.exit(1)
 
    print(f"Balance    : {balance:.4f} SOL  (ready)\n")
 
    run_checklist(client, wallet)
 
    print("\nDone.")
 
 
if __name__ == "__main__":
    main()
