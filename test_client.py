"""
Solana Crowdfunding Platform - Python Test Client (Fixed)

This client lets any user connect their Solana wallet on Devnet and interact
with the crowdfunding smart contract.

Program ID: 3Dc6ZJsWiQm6CmDUt5MY4izbdLgpBU2KbhfSmqpVcayM

Usage:
    pip install solders solana

    # Option 1: Use your default Solana CLI keypair
    python test_client.py

    # Option 2: Specify a keypair file
    python test_client.py --keypair /path/to/keypair.json

    # Option 3: Set environment variable
    export SOLANA_KEYPAIR_PATH=/path/to/keypair.json
    python test_client.py

    # Option 4: Generate a fresh throwaway keypair
    python test_client.py --new-wallet

Requires SOL on Devnet. The script auto-requests an airdrop if your balance
is below 0.5 SOL.
"""

import argparse
import json
import os
import re
import struct
import sys
import time

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

PROGRAM_ID = Pubkey.from_string("3Dc6ZJsWiQm6CmDUt5MY4izbdLgpBU2KbhfSmqpVcayM")
RPC_URL = "https://api.devnet.solana.com"
RENT_SYSVAR = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

# ---------------------------------------------------------------------------
# Wallet loading — supports 4 sources
# ---------------------------------------------------------------------------

def _default_keypair_paths():
    """Return platform-appropriate default keypair search paths."""
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".config", "solana", "id.json"),   # Linux/macOS standard
        os.path.join(home, "solana", "id.json"),               # Windows fallback
        os.path.join(home, ".solana", "id.json"),              # Alternative
        "id.json",                                              # Current directory
    ]


def load_wallet(keypair_path=None, generate_new=False):
    """
    Load a Keypair from one of several sources (in priority order):
      1. Explicit --keypair argument
      2. SOLANA_KEYPAIR_PATH environment variable
      3. Default Solana CLI path (~/.config/solana/id.json + OS variants)
      4. Generate a throwaway keypair (--new-wallet flag)

    The keypair JSON must be the standard 64-byte array from solana-keygen.
    """
    if keypair_path:
        return _load_from_file(keypair_path)

    env_path = os.environ.get("SOLANA_KEYPAIR_PATH")
    if env_path:
        print(f"Using keypair from env SOLANA_KEYPAIR_PATH: {env_path}")
        return _load_from_file(env_path)

    for path in _default_keypair_paths():
        if os.path.exists(path):
            print(f"Found keypair at: {path}")
            return _load_from_file(path)

    if generate_new:
        kp = Keypair()
        print("Generated new throwaway keypair (NOT persisted to disk).")
        print(f"  Public key: {kp.pubkey()}")
        print("  WARNING: Funds will be lost when this script exits.")
        return kp

    print("ERROR: No Solana keypair found.")
    print()
    print("Options:")
    print("  A) Create one with the Solana CLI:   solana-keygen new")
    print("  B) Pass a file:  python test_client.py --keypair /path/to/id.json")
    print("  C) Set env var:  export SOLANA_KEYPAIR_PATH=/path/to/id.json")
    print("  D) Throwaway:    python test_client.py --new-wallet")
    sys.exit(1)


def _load_from_file(path):
    """Load a Keypair from a standard Solana CLI JSON file (64-byte array)."""
    if not os.path.exists(path):
        print(f"ERROR: Keypair file not found: {path}")
        sys.exit(1)
    with open(path, "r") as f:
        raw = json.load(f)
    if not isinstance(raw, list) or len(raw) != 64:
        print(f"ERROR: Expected a 64-element JSON array, got {len(raw)} elements.")
        sys.exit(1)
    return Keypair.from_bytes(bytes(raw))


# ---------------------------------------------------------------------------
# Devnet helpers
# ---------------------------------------------------------------------------

def get_client():
    """Return a Solana RPC client pointed at Devnet."""
    c = Client(RPC_URL)
    try:
        c.get_version()
    except Exception as e:
        print(f"ERROR: Cannot reach Devnet ({RPC_URL}): {e}")
        sys.exit(1)
    return c


def get_balance_sol(client, pubkey):
    return client.get_balance(pubkey, commitment=Confirmed).value / 1_000_000_000


def ensure_funded(client, pubkey, min_sol=0.5):
    """Auto-airdrop 2 SOL on Devnet if balance is below min_sol."""
    balance = get_balance_sol(client, pubkey)
    print(f"Wallet balance: {balance:.4f} SOL")
    if balance >= min_sol:
        return

    print(f"Balance below {min_sol} SOL — requesting 2 SOL airdrop...")
    for attempt in range(1, 3):
        try:
            resp = client.request_airdrop(pubkey, 2_000_000_000, commitment=Confirmed)
            sig = resp.value
            print(f"  Airdrop requested (attempt {attempt}). Sig: {sig}")
            for _ in range(30):
                time.sleep(1)
                status = client.get_signature_statuses([sig]).value[0]
                if status and status.confirmation_status:
                    balance = get_balance_sol(client, pubkey)
                    print(f"  Confirmed! New balance: {balance:.4f} SOL")
                    return
        except Exception as e:
            print(f"  Airdrop attempt {attempt} failed: {e}")
            time.sleep(5)

    print("WARNING: Could not auto-airdrop. Run: solana airdrop 2 --url devnet")


# ---------------------------------------------------------------------------
# Instruction builders  (exact match to src/lib.rs)
# ---------------------------------------------------------------------------

def ix_create_campaign(creator, campaign, goal, deadline):
    """
    Instruction 0 — CreateCampaign
    Accounts: creator(signer,w), campaign(signer,w), system_program, rent_sysvar
    Data: [0x00] + goal(u64 LE) + deadline(i64 LE)
    """
    data = bytes([0]) + struct.pack("<Q", goal) + struct.pack("<q", deadline)
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=creator, is_signer=True, is_writable=True),
            AccountMeta(pubkey=campaign, is_signer=True, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=RENT_SYSVAR, is_signer=False, is_writable=False),
        ],
        data=data,
    )


def ix_contribute(donor, campaign, amount):
    """
    Instruction 1 — Contribute
    Accounts: donor(signer,w), campaign(w), system_program
    Data: [0x01] + amount(u64 LE)
    """
    data = bytes([1]) + struct.pack("<Q", amount)
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=donor, is_signer=True, is_writable=True),
            AccountMeta(pubkey=campaign, is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
        data=data,
    )


def ix_withdraw(creator, campaign):
    """
    Instruction 2 — Withdraw
    Accounts: creator(signer,w), campaign(w)
    Data: [0x02]
    """
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=creator, is_signer=True, is_writable=True),
            AccountMeta(pubkey=campaign, is_signer=False, is_writable=True),
        ],
        data=bytes([2]),
    )


def ix_refund(donor, campaign, amount):
    """
    Instruction 3 — Refund
    Accounts: donor(signer,w), campaign(w)
    Data: [0x03] + amount(u64 LE)
    """
    data = bytes([3]) + struct.pack("<Q", amount)
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=donor, is_signer=True, is_writable=True),
            AccountMeta(pubkey=campaign, is_signer=False, is_writable=True),
        ],
        data=data,
    )


# ---------------------------------------------------------------------------
# Transaction helpers
# ---------------------------------------------------------------------------

def send_tx(client, instructions, signers, label="tx"):
    """
    Build and send a transaction.
    signers[0] is the fee payer.
    Returns the signature string on success, None on failure.

    FIX: On failure, parses and prints the on-chain program logs so you
    can see the real error reason (e.g. CampaignActive, AlreadyClaimed).
    """
    blockhash = client.get_latest_blockhash(commitment=Confirmed).value.blockhash
    msg = Message.new_with_blockhash(
        instructions,
        signers[0].pubkey(),
        blockhash,
    )
    tx = Transaction(
        from_keypairs=signers,
        message=msg,
        recent_blockhash=blockhash,
    )
    try:
        opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        resp = client.send_transaction(tx, opts=opts)
        sig = str(resp.value)
        print(f"  [{label}] Signature: {sig}")
        print(f"           Explorer: https://explorer.solana.com/tx/{sig}?cluster=devnet")
        return sig
    except Exception as e:
        err_str = str(e)
        print(f"  [{label}] FAILED: {err_str}")
        # Extract and print on-chain program logs for easier debugging
        if "logs" in err_str.lower():
            try:
                logs = re.findall(r'Program log: (.*?)(?:\\n|")', err_str)
                if logs:
                    print(f"  [{label}] On-chain program logs:")
                    for log in logs:
                        print(f"    >> {log}")
            except Exception:
                pass
        return None


def wait_confirm(client, sig, timeout=60):
    """
    Poll for transaction confirmation. Returns True if confirmed.

    FIX 1: Timeout extended from 30s → 60s to handle slow Devnet slots.
    FIX 2: confirmation_status is a solders enum, not a plain string.
            We convert it with str() and use 'in' instead of == to avoid
            silent mismatches (e.g. TransactionConfirmationStatus.Confirmed
            would never equal the string "confirmed").
    """
    if not sig:
        return False
    for i in range(timeout):
        time.sleep(1)
        try:
            status = client.get_signature_statuses([sig]).value[0]
            if status is not None:
                cs = status.confirmation_status
                # Convert enum to lowercase string for safe comparison
                cs_str = str(cs).lower() if cs else ""
                if "confirmed" in cs_str or "finalized" in cs_str:
                    return True
        except Exception as e:
            # Log polling errors every 10s so the console isn't spammed
            if i % 10 == 0:
                print(f"  Polling ({i}s elapsed)... ({e})")
    print(f"  WARNING: Timed out waiting for confirmation after {timeout}s")
    return False


def check_tx_status(client, sig):
    """
    NEW: Fetch the full transaction result from the RPC and print its
    outcome + program logs. Call this whenever wait_confirm returns False
    so you can tell the difference between:
      - TX confirmed on-chain but client timed out polling  (happy path)
      - TX landed but the program rejected it               (on-chain error)
      - TX genuinely not yet processed                      (retry / give up)
    """
    if not sig:
        return
    print(f"  Checking on-chain status for: {sig}")
    try:
        result = client.get_transaction(
            sig,
            max_supported_transaction_version=0,
            commitment=Confirmed,
        )
        if result.value is None:
            print("  TX not found on-chain yet — Devnet may still be processing.")
            return

        meta = result.value.transaction.meta
        if meta is None:
            print("  TX found but metadata unavailable.")
            return

        if meta.err:
            print(f"  TX landed but FAILED on-chain: {meta.err}")
        else:
            print(f"  TX confirmed on-chain ✓  (fee: {meta.fee} lamports)")

        if meta.log_messages:
            print("  Program logs:")
            for log in meta.log_messages:
                print(f"    >> {log}")

    except Exception as e:
        print(f"  Status check error: {e}")


# ---------------------------------------------------------------------------
# Test scenario 1: Successful campaign
# ---------------------------------------------------------------------------

def test_success_scenario(client, wallet):
    print("\n" + "="*60)
    print("SCENARIO 1: Successful Campaign (goal reached → withdraw)")
    print("="*60)

    campaign_kp = Keypair()
    goal = 100_000_000        # 0.1 SOL goal
    deadline = int(time.time()) + 20  # 20 seconds from now

    print(f"\n[1/7] Create campaign")
    print(f"      Goal:     {goal / 1e9} SOL")
    print(f"      Deadline: {deadline} (~20 seconds from now)")
    print(f"      Campaign: {campaign_kp.pubkey()}")
    sig = send_tx(
        client,
        [ix_create_campaign(wallet.pubkey(), campaign_kp.pubkey(), goal, deadline)],
        [wallet, campaign_kp],
        "create_campaign",
    )
    if not wait_confirm(client, sig):
        # FIX: Instead of blindly aborting, check what actually happened on-chain.
        # The TX may have confirmed after our polling window — fetch the real status.
        print("  Confirmation polling timed out — fetching on-chain status...")
        check_tx_status(client, sig)
        print("  Giving Devnet 10 more seconds then retrying status check...")
        time.sleep(10)
        check_tx_status(client, sig)
        print("  Aborting scenario 1. Check the Explorer link above for details.")
        return

    print("\n[2/7] Contribute 0.07 SOL")
    sig = send_tx(
        client,
        [ix_contribute(wallet.pubkey(), campaign_kp.pubkey(), 70_000_000)],
        [wallet],
        "contribute_1",
    )
    wait_confirm(client, sig)

    print("\n[3/7] Contribute 0.05 SOL  (total 0.12 SOL > 0.1 SOL goal)")
    sig = send_tx(
        client,
        [ix_contribute(wallet.pubkey(), campaign_kp.pubkey(), 50_000_000)],
        [wallet],
        "contribute_2",
    )
    wait_confirm(client, sig)

    print("\n[4/7] Withdraw BEFORE deadline (expected: CampaignActive error ✗)")
    sig = send_tx(
        client,
        [ix_withdraw(wallet.pubkey(), campaign_kp.pubkey())],
        [wallet],
        "early_withdraw",
    )
    print("      " + ("UNEXPECTED: succeeded" if sig else "Correct: rejected ✓"))

    print("\n[5/7] Waiting for deadline (20s)...")
    time.sleep(23)

    print("\n[6/7] Withdraw after deadline (expected: success ✓)")
    sig = send_tx(
        client,
        [ix_withdraw(wallet.pubkey(), campaign_kp.pubkey())],
        [wallet],
        "withdraw",
    )
    wait_confirm(client, sig)
    print("      " + ("Withdraw succeeded ✓" if sig else "UNEXPECTED: failed"))

    print("\n[7/7] Withdraw again (expected: AlreadyClaimed error ✗)")
    sig = send_tx(
        client,
        [ix_withdraw(wallet.pubkey(), campaign_kp.pubkey())],
        [wallet],
        "double_withdraw",
    )
    print("      " + ("UNEXPECTED: succeeded" if sig else "Correct: rejected ✓"))

    print("\n[Scenario 1 done]")


# ---------------------------------------------------------------------------
# Test scenario 2: Failed campaign → refund
# ---------------------------------------------------------------------------

def test_refund_scenario(client, wallet):
    print("\n" + "="*60)
    print("SCENARIO 2: Failed Campaign (goal not reached → refund)")
    print("="*60)

    campaign_kp = Keypair()
    goal = 500_000_000        # 0.5 SOL goal (we will NOT reach it)
    deadline = int(time.time()) + 20

    print(f"\n[1/5] Create campaign")
    print(f"      Goal:     {goal / 1e9} SOL (we will only contribute 0.05 SOL)")
    print(f"      Campaign: {campaign_kp.pubkey()}")
    sig = send_tx(
        client,
        [ix_create_campaign(wallet.pubkey(), campaign_kp.pubkey(), goal, deadline)],
        [wallet, campaign_kp],
        "create_campaign",
    )
    if not wait_confirm(client, sig):
        # FIX: Same as scenario 1 — diagnose before aborting.
        print("  Confirmation polling timed out — fetching on-chain status...")
        check_tx_status(client, sig)
        print("  Giving Devnet 10 more seconds then retrying status check...")
        time.sleep(10)
        check_tx_status(client, sig)
        print("  Aborting scenario 2. Check the Explorer link above for details.")
        return

    contrib = 50_000_000   # 0.05 SOL
    print(f"\n[2/5] Contribute 0.05 SOL (far below 0.5 SOL goal)")
    sig = send_tx(
        client,
        [ix_contribute(wallet.pubkey(), campaign_kp.pubkey(), contrib)],
        [wallet],
        "contribute",
    )
    wait_confirm(client, sig)

    print("\n[3/5] Try withdraw before deadline (expected: CampaignActive error ✗)")
    sig = send_tx(
        client,
        [ix_withdraw(wallet.pubkey(), campaign_kp.pubkey())],
        [wallet],
        "early_withdraw",
    )
    print("      " + ("UNEXPECTED: succeeded" if sig else "Correct: rejected ✓"))

    print("\n[4/5] Waiting for deadline (20s)...")
    time.sleep(23)

    print("\n[5/5] Refund 0.05 SOL (expected: success ✓)")
    sig = send_tx(
        client,
        [ix_refund(wallet.pubkey(), campaign_kp.pubkey(), contrib)],
        [wallet],
        "refund",
    )
    wait_confirm(client, sig)
    print("      " + ("Refund succeeded ✓" if sig else "UNEXPECTED: failed"))

    print("\n[Scenario 2 done]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Solana Crowdfunding — Devnet test client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--keypair", metavar="PATH",
                   help="Path to a Solana CLI keypair JSON file (64-byte array).")
    p.add_argument("--new-wallet", action="store_true",
                   help="Generate a throwaway keypair for this run.")
    p.add_argument("--scenario", choices=["success", "refund", "all"], default="all",
                   help="Test scenario to run (default: all).")
    return p.parse_args()


def main():
    args = parse_args()

    print("=" * 60)
    print("Solana Crowdfunding — Devnet Test Client")
    print("=" * 60)
    print(f"Program ID : {PROGRAM_ID}")
    print(f"RPC URL    : {RPC_URL}")
    print()

    wallet = load_wallet(keypair_path=args.keypair, generate_new=args.new_wallet)
    print(f"Wallet     : {wallet.pubkey()}")

    client = get_client()
    print("Devnet     : connected")

    ensure_funded(client, wallet.pubkey(), min_sol=0.5)

    balance = get_balance_sol(client, wallet.pubkey())
    if balance < 0.3:
        print(f"\nERROR: Need at least 0.3 SOL. Current: {balance:.4f} SOL")
        print("  Run: solana airdrop 2 --url devnet")
        sys.exit(1)

    print(f"Balance    : {balance:.4f} SOL  (ready)")

    if args.scenario in ("success", "all"):
        test_success_scenario(client, wallet)
    if args.scenario in ("refund", "all"):
        test_refund_scenario(client, wallet)

    print("\n" + "=" * 60)
    print("All tests finished.")
    print("=" * 60)


if __name__ == "__main__":
    main()
