"""
Solana Crowdfunding — Devnet Test Client (PDA Vault + Contribution Tracking)

Usage:
    pip install solders solana

    python test_client.py                           # default keypair
    python test_client.py --keypair /path/id.json   # explicit
    python test_client.py --new-wallet              # throwaway + airdrop
    python test_client.py --scenario success|refund|all

Architecture (after security fixes):
─────────────────────────────────────
  campaign account   — owned by the program, stores Campaign state
  vault PDA          — seeds ["vault", campaign], holds all SOL
  contribution PDA   — seeds ["contribution", campaign, donor], tracks per-donor amount

Refund no longer accepts an arbitrary amount — it reads the on-chain Contribution
record and refunds exactly what the donor contributed.  The Contribution account
is closed (zeroed + rent returned) after refund to prevent double-refund.

CreateCampaign no longer needs rent_sysvar (uses Rent::get()).  Vault is
initialised via transfer+allocate+assign to prevent pre-funding attacks.
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
# Config
# =============================================================================

PROGRAM_ID       = Pubkey.from_string("DKsRhfniEEv3EcNgvbid11aDAAC3Mbsxui3rTQnU5GS3")
RPC_URL          = "https://api.devnet.solana.com"
LAMPORTS_PER_SOL = 1_000_000_000
DEADLINE_SECS    = 45

GOAL_SOL      = 0.10
CONTRIB_1_SOL = 0.06
CONTRIB_2_SOL = 0.05

# =============================================================================
# PDA derivation — mirrors lib.rs exactly
# =============================================================================

def find_vault_pda(campaign: Pubkey) -> tuple:
    """Seeds: [b"vault", campaign_pubkey]"""
    return Pubkey.find_program_address([b"vault", bytes(campaign)], PROGRAM_ID)

def find_contribution_pda(campaign: Pubkey, donor: Pubkey) -> tuple:
    """Seeds: [b"contribution", campaign_pubkey, donor_pubkey]"""
    return Pubkey.find_program_address(
        [b"contribution", bytes(campaign), bytes(donor)], PROGRAM_ID
    )

# =============================================================================
# Wallet
# =============================================================================

def _default_keypair_paths() -> List[str]:
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".config", "solana", "id.json"),
        os.path.join(home, "solana", "id.json"),
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
    for p in _default_keypair_paths():
        if os.path.exists(p):
            print(f"Found keypair at: {p}")
            return _load_from_file(p)
    if generate_new:
        kp = Keypair()
        print(f"Generated throwaway keypair: {kp.pubkey()}")
        return kp
    print("ERROR: No keypair found. Use --keypair or --new-wallet.")
    sys.exit(1)

def _load_from_file(path: str) -> Keypair:
    if not os.path.exists(path):
        print(f"ERROR: Not found: {path}"); sys.exit(1)
    with open(path) as f:
        raw = json.load(f)
    if not isinstance(raw, list) or len(raw) != 64:
        print(f"ERROR: Bad keypair file."); sys.exit(1)
    return Keypair.from_bytes(bytes(raw))

# =============================================================================
# RPC helpers
# =============================================================================

def get_client() -> Client:
    c = Client(RPC_URL)
    try: c.get_version()
    except Exception as e:
        print(f"ERROR: Cannot reach Devnet: {e}"); sys.exit(1)
    return c

def sol(client, pk) -> float:
    return client.get_balance(pk, commitment=Confirmed).value / LAMPORTS_PER_SOL

def ensure_funded(client, pk, min_sol=0.5):
    bal = sol(client, pk)
    print(f"Balance: {bal:.4f} SOL")
    if bal >= min_sol:
        return
    print(f"Requesting 2 SOL airdrop...")
    for attempt in range(1, 4):
        try:
            sig = client.request_airdrop(pk, 2 * LAMPORTS_PER_SOL, commitment=Confirmed).value
            for _ in range(45):
                time.sleep(1)
                st = client.get_signature_statuses([sig]).value[0]
                if st and st.confirmation_status:
                    print(f"  Confirmed! Balance: {sol(client, pk):.4f} SOL")
                    return
        except Exception as e:
            print(f"  Attempt {attempt}: {e}")
            time.sleep(5)
    print("WARNING: Airdrop failed. Run: solana airdrop 2 --url devnet")

# =============================================================================
# TX helpers
# =============================================================================

def send_tx(client, ixs, signers, label="tx") -> Optional[Signature]:
    try:
        bh  = client.get_latest_blockhash(commitment=Confirmed).value.blockhash
        msg = MessageV0.try_compile(signers[0].pubkey(), ixs, [], bh)
        tx  = VersionedTransaction(msg, signers)
        sig = client.send_transaction(tx, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)).value
        print(f"  [{label}] sig: {sig}")
        print(f"  [{label}] https://explorer.solana.com/tx/{sig}?cluster=devnet")
        return sig
    except Exception as e:
        err = str(e)
        print(f"  [{label}] FAILED: {err[:400]}")
        logs = re.findall(r'"Program log: (.*?)"', err)
        for l in logs:
            print(f"    >> {l}")
        return None

def wait_confirm(client, sig, timeout=60) -> bool:
    if not sig: return False
    for _ in range(timeout):
        time.sleep(1)
        try:
            st = client.get_signature_statuses([sig]).value[0]
            if st:
                cs = str(st.confirmation_status).lower() if st.confirmation_status else ""
                if "confirmed" in cs or "finalized" in cs:
                    return True
        except: pass
    print(f"  WARNING: Timed out ({timeout}s)")
    return False

def sac(client, ixs, signers, label) -> bool:
    sig = send_tx(client, ixs, signers, label)
    return sig is not None and wait_confirm(client, sig)

# =============================================================================
# Instruction builders — match new lib.rs account layouts EXACTLY
# =============================================================================

def ix_create_campaign(creator, campaign, vault, goal, deadline) -> Instruction:
    """
    Accounts: creator(sw), campaign(sw), vault(w), system_program
    NOTE: rent_sysvar removed — program uses Rent::get()
    """
    data = bytes([0]) + struct.pack("<Q", goal) + struct.pack("<q", deadline)
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=creator,           is_signer=True,  is_writable=True),
            AccountMeta(pubkey=campaign,           is_signer=True,  is_writable=True),
            AccountMeta(pubkey=vault,              is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
        data=data,
    )

def ix_contribute(donor, campaign, vault, contribution, amount) -> Instruction:
    """
    Accounts: donor(sw), campaign(w), vault(w), contribution(w), system_program
    """
    data = bytes([1]) + struct.pack("<Q", amount)
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=donor,             is_signer=True,  is_writable=True),
            AccountMeta(pubkey=campaign,           is_signer=False, is_writable=True),
            AccountMeta(pubkey=vault,              is_signer=False, is_writable=True),
            AccountMeta(pubkey=contribution,       is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
        data=data,
    )

def ix_withdraw(creator, campaign, vault) -> Instruction:
    """
    Accounts: creator(sw), campaign(w), vault(w), system_program
    """
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

def ix_refund(donor, campaign, vault, contribution) -> Instruction:
    """
    Accounts: donor(sw), campaign(w), vault(w), contribution(w), system_program
    NO amount in data — program reads Contribution PDA to determine refund amount.
    """
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=donor,             is_signer=True,  is_writable=True),
            AccountMeta(pubkey=campaign,           is_signer=False, is_writable=True),
            AccountMeta(pubkey=vault,              is_signer=False, is_writable=True),
            AccountMeta(pubkey=contribution,       is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
        data=bytes([3]),
    )

# =============================================================================
# Result tracking
# =============================================================================

@dataclass
class StepResult:
    number: int; label: str; passed: bool; note: str = ""

_results: list = []

def record(n, label, passed, note=""):
    _results.append(StepResult(n, label, passed, note))
    icon = "PASS" if passed else "FAIL"
    print(f"  [{'OK' if passed else 'XX'} {icon}] {label}")
    if note: print(f"         -> {note}")

def print_summary():
    print("\n" + "=" * 66)
    print("TEST SUMMARY")
    print("=" * 66)
    for r in _results:
        s = "PASS" if r.passed else "FAIL"
        print(f"  {r.number:>2}. {r.label:<44} {s}")
        if not r.passed and r.note:
            print(f"       -> {r.note}")
    p = sum(1 for r in _results if r.passed)
    print("-" * 66)
    print(f"  {p}/{len(_results)} passed")
    print("=" * 66)

# =============================================================================
# Scenario 1 — Success: goal reached -> withdraw
# =============================================================================

def run_success(client, wallet):
    print("\n" + "=" * 66)
    print("SCENARIO 1: Goal Reached -> Withdraw")
    print("=" * 66)

    ckp = Keypair()
    cpub = ckp.pubkey()
    vpub, _ = find_vault_pda(cpub)
    wpub = wallet.pubkey()
    # Contribution PDA for wallet (acts as both creator and donor in test)
    conpub, _ = find_contribution_pda(cpub, wpub)

    goal  = int(GOAL_SOL * LAMPORTS_PER_SOL)
    c1    = int(CONTRIB_1_SOL * LAMPORTS_PER_SOL)
    c2    = int(CONTRIB_2_SOL * LAMPORTS_PER_SOL)
    dl    = int(time.time()) + DEADLINE_SECS

    print(f"\n  Creator       : {wpub}")
    print(f"  Campaign      : {cpub}")
    print(f"  Vault PDA     : {vpub}")
    print(f"  Contribution  : {conpub}")
    print(f"  Goal          : {GOAL_SOL} SOL")
    print(f"  Deadline      : ~{DEADLINE_SECS}s")

    # 1 Create
    print("\n--- STEP 1: Create campaign")
    ok = sac(client, [ix_create_campaign(wpub, cpub, vpub, goal, dl)], [wallet, ckp], "create")
    record(1, f"Create campaign (goal={GOAL_SOL} SOL)", ok)
    if not ok: return

    # 2 Contribute 0.06
    print(f"\n--- STEP 2: Contribute {CONTRIB_1_SOL} SOL")
    vb = sol(client, vpub)
    ok = sac(client, [ix_contribute(wpub, cpub, vpub, conpub, c1)], [wallet], "contrib_1")
    va = sol(client, vpub)
    d = va - vb
    print(f"  Vault: {vb:.6f} -> {va:.6f} (+{d:.6f})")
    record(2, f"Contribute {CONTRIB_1_SOL} SOL -> vault", ok and d >= CONTRIB_1_SOL * 0.99)

    # 3 Contribute 0.05
    print(f"\n--- STEP 3: Contribute {CONTRIB_2_SOL} SOL (total > goal)")
    vb = sol(client, vpub)
    ok = sac(client, [ix_contribute(wpub, cpub, vpub, conpub, c2)], [wallet], "contrib_2")
    va = sol(client, vpub)
    d = va - vb
    print(f"  Vault: {vb:.6f} -> {va:.6f} (+{d:.6f})")
    record(3, f"Contribute {CONTRIB_2_SOL} SOL -> vault > goal", ok and d >= CONTRIB_2_SOL * 0.99)

    # 4 Early withdraw (must fail)
    print("\n--- STEP 4: Withdraw before deadline (expect FAIL)")
    sig = send_tx(client, [ix_withdraw(wpub, cpub, vpub)], [wallet], "early_wd")
    if sig is None:
        record(4, "Early withdraw rejected", True, "Preflight rejected")
    else:
        early_ok = wait_confirm(client, sig, 20)
        record(4, "Early withdraw rejected", not early_ok)

    # 5 Wait + withdraw
    print("\n--- STEP 5: Wait for deadline then withdraw")
    rem = dl - int(time.time())
    if rem > 0:
        w = rem + 3
        for t in range(w, 0, -1):
            print(f"  {t:>3}s ...", end="\r"); time.sleep(1)
        print()

    vb = sol(client, vpub); cb = sol(client, wpub)
    ok = sac(client, [ix_withdraw(wpub, cpub, vpub)], [wallet], "withdraw")
    va = sol(client, vpub); ca = sol(client, wpub)
    print(f"  Vault:   {vb:.6f} -> {va:.6f}")
    print(f"  Creator: {cb:.6f} -> {ca:.6f}")
    record(5, "Withdraw after deadline -> success", ok and va < vb)

    # 6 Double withdraw (must fail)
    print("\n--- STEP 6: Double withdraw (expect FAIL)")
    sig = send_tx(client, [ix_withdraw(wpub, cpub, vpub)], [wallet], "double_wd")
    if sig is None:
        record(6, "Double withdraw rejected (AlreadyClaimed)", True)
    else:
        ok2 = wait_confirm(client, sig, 20)
        record(6, "Double withdraw rejected (AlreadyClaimed)", not ok2)

# =============================================================================
# Scenario 2 — Fail: goal not reached -> refund
# =============================================================================

def run_refund(client, wallet):
    print("\n" + "=" * 66)
    print("SCENARIO 2: Goal NOT Reached -> Refund")
    print("=" * 66)

    ckp = Keypair()
    cpub = ckp.pubkey()
    vpub, _ = find_vault_pda(cpub)
    wpub = wallet.pubkey()
    conpub, _ = find_contribution_pda(cpub, wpub)

    goal = int(0.50 * LAMPORTS_PER_SOL)
    amt  = int(0.05 * LAMPORTS_PER_SOL)
    dl   = int(time.time()) + DEADLINE_SECS

    print(f"\n  Campaign      : {cpub}")
    print(f"  Vault PDA     : {vpub}")
    print(f"  Contribution  : {conpub}")
    print(f"  Goal          : 0.5 SOL (will only contribute 0.05)")

    # 1 Create
    print("\n--- STEP 1: Create campaign")
    ok = sac(client, [ix_create_campaign(wpub, cpub, vpub, goal, dl)], [wallet, ckp], "create")
    record(7, "Create campaign (goal=0.5 SOL)", ok)
    if not ok: return

    # 2 Contribute 0.05
    print("\n--- STEP 2: Contribute 0.05 SOL")
    ok = sac(client, [ix_contribute(wpub, cpub, vpub, conpub, amt)], [wallet], "contrib")
    vbal = sol(client, vpub)
    print(f"  Vault: {vbal:.6f} SOL")
    record(8, "Contribute 0.05 SOL -> vault", ok and vbal > 0)

    # 3 Wait + withdraw attempt (must fail - goal not reached)
    print("\n--- STEP 3: Wait + withdraw (expect FAIL - GoalNotReached)")
    rem = dl - int(time.time())
    if rem > 0:
        w = rem + 3
        for t in range(w, 0, -1):
            print(f"  {t:>3}s ...", end="\r"); time.sleep(1)
        print()
    sig = send_tx(client, [ix_withdraw(wpub, cpub, vpub)], [wallet], "wd_no_goal")
    if sig is None:
        record(9, "Withdraw (goal not met) -> rejected", True)
    else:
        ok2 = wait_confirm(client, sig, 20)
        record(9, "Withdraw (goal not met) -> rejected", not ok2)

    # 4 Refund (no amount arg — program reads Contribution PDA)
    print("\n--- STEP 4: Refund (expect SUCCESS)")
    vb = sol(client, vpub); db = sol(client, wpub)
    ok = sac(client, [ix_refund(wpub, cpub, vpub, conpub)], [wallet], "refund")
    va = sol(client, vpub); da = sol(client, wpub)
    print(f"  Vault: {vb:.6f} -> {va:.6f}")
    print(f"  Donor: {db:.6f} -> {da:.6f}")
    record(10, "Refund donor -> success", ok and va < vb)

    # 5 Double refund (must fail — contribution account closed)
    print("\n--- STEP 5: Double refund (expect FAIL)")
    sig = send_tx(client, [ix_refund(wpub, cpub, vpub, conpub)], [wallet], "double_ref")
    if sig is None:
        record(11, "Double refund rejected", True, "Contribution account closed")
    else:
        ok2 = wait_confirm(client, sig, 20)
        record(11, "Double refund rejected", not ok2)

# =============================================================================
# CLI
# =============================================================================

def main():
    p = argparse.ArgumentParser(description="Solana Crowdfunding Devnet Test Client")
    p.add_argument("--keypair", metavar="PATH")
    p.add_argument("--new-wallet", action="store_true")
    p.add_argument("--scenario", choices=["success", "refund", "all"], default="all")
    args = p.parse_args()

    print("=" * 66)
    print("Solana Crowdfunding — Devnet Test [PDA Vault + Contribution Tracking]")
    print("=" * 66)
    print(f"Program : {PROGRAM_ID}")
    print(f"RPC     : {RPC_URL}\n")

    wallet = load_wallet(args.keypair, args.new_wallet)
    print(f"Wallet  : {wallet.pubkey()}")

    client = get_client()
    print("Devnet  : connected")
    ensure_funded(client, wallet.pubkey())

    bal = sol(client, wallet.pubkey())
    if bal < 0.3:
        print(f"ERROR: Need 0.3+ SOL. Have: {bal:.4f}"); sys.exit(1)
    print(f"Balance : {bal:.4f} SOL\n")

    if args.scenario in ("success", "all"):
        run_success(client, wallet)
    if args.scenario in ("refund", "all"):
        run_refund(client, wallet)

    print_summary()

if __name__ == "__main__":
    main()
