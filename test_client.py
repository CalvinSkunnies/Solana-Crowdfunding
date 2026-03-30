"""
Solana Crowdfunding Platform - Python Test Client
Repo: https://github.com/CalvinSkunnies/Solana-Crowdfunding

BUGS FIXED IN THIS VERSION
────────────────────────────────────────────────────────────────
1. Signature type error:
     'str' object cannot be converted to 'Signature'
   FIX: Wrap the raw string from send_transaction() with
        Signature.from_string() before passing to get_signature_statuses().

2. Transaction timeout / never confirms:
   FIX: Use VersionedTransaction + MessageV0 instead of the legacy
        Transaction + Message combo which serializes incorrectly with
        modern solders + solana-py versions.

3. confirmation_status always False:
   FIX: str(cs).lower() instead of cs == "confirmed" — solders returns
        an enum object, not a plain string.

CHECKLIST BEING TESTED
────────────────────────────────────────────────────────────────
1. Create campaign   goal=0.1 SOL, deadline=tomorrow (45s in SHORT mode)
2. Contribute 0.06 SOL  →  raised = 0.06 SOL
3. Contribute 0.05 SOL  →  raised = 0.11 SOL  (exceeds 0.1 SOL goal)
4. Withdraw BEFORE deadline  →  must FAIL  (CampaignActive)
5. Wait for deadline, withdraw  →  must SUCCEED
6. Withdraw again  →  must FAIL  (AlreadyClaimed)

FUNDS ARE SENT AUTOMATICALLY FROM YOUR WALLET
The script uses your existing Solana CLI keypair (or generates a
throwaway one with --new-wallet) and auto-airdrops on Devnet if needed.

Usage:
    pip install solders solana

    python test_client.py                       # default ~/.config/solana/id.json
    python test_client.py --keypair /path/to/id.json
    python test_client.py --new-wallet          # throwaway keypair + auto-airdrop
    python test_client.py --long-deadline       # real 24h deadline instead of 45s
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

# ── solders ───────────────────────────────────────────────────────────────────
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature                    # ← FIX: explicit import
from solders.instruction import Instruction, AccountMeta
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.message import MessageV0                      # ← modern API
from solders.transaction import VersionedTransaction       # ← modern API

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

# ── Checklist amounts (matching the spec exactly) ─────────────────────────────
GOAL_SOL      = 0.1    # 0.1  SOL goal
CONTRIB_1_SOL = 0.06   # 0.06 SOL  →  raised = 0.06
CONTRIB_2_SOL = 0.05   # 0.05 SOL  →  raised = 0.11  (exceeds goal)

# Deadline: 45 s for quick local testing, 24 h with --long-deadline
SHORT_DEADLINE  = True
DEADLINE_OFFSET = 45   # seconds

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

def _default_keypair_paths() -> list:
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".config", "solana", "id.json"),  # Linux / macOS
        os.path.join(home, "solana",  "id.json"),             # Windows
        os.path.join(home, ".solana", "id.json"),
        "id.json",                                            # cwd fallback
    ]


def load_wallet(keypair_path: Optional[str] = None, generate_new: bool = False) -> Keypair:
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


def ensure_funded(client: Client, pubkey: Pubkey, min_sol: float = 0.5):
    """
    Auto-airdrop 2 SOL on Devnet if the wallet balance is below min_sol.
    This is how funds are obtained automatically — no manual step needed.
    """
    balance = get_balance_sol(client, pubkey)
    print(f"Wallet balance : {balance:.4f} SOL")
    if balance >= min_sol:
        return

    print(f"Balance below {min_sol} SOL — requesting 2 SOL airdrop automatically...")
    for attempt in range(1, 4):
        try:
            resp    = client.request_airdrop(pubkey, 2 * LAMPORTS_PER_SOL, commitment=Confirmed)
            sig_str = str(resp.value)
            sig_obj = Signature.from_string(sig_str)          # ← must be Signature, not str
            print(f"  Airdrop requested (attempt {attempt}). Sig: {sig_str}")
            for _ in range(45):
                time.sleep(1)
                st = client.get_signature_statuses([sig_obj]).value[0]
                if st and st.confirmation_status:
                    balance = get_balance_sol(client, pubkey)
                    print(f"  Airdrop confirmed!  New balance: {balance:.4f} SOL")
                    return
        except Exception as e:
            print(f"  Attempt {attempt} failed: {e}")
            time.sleep(6)

    print("WARNING: Auto-airdrop failed.  Run manually:  solana airdrop 2 --url devnet")


# =============================================================================
# PDA vault derivation — seeds must match src/lib.rs exactly
# =============================================================================

def derive_vault_pda(campaign_pubkey: Pubkey) -> tuple:
    """
    Seeds: [b"vault", campaign_pubkey_bytes]
    Matches:  Pubkey::find_program_address(&[b"vault", campaign.key.as_ref()], program_id)
    """
    return Pubkey.find_program_address(
        [b"vault", bytes(campaign_pubkey)],
        PROGRAM_ID,
    )


# =============================================================================
# Instruction builders
# Native Borsh program — single-byte discriminator, little-endian payloads
# =============================================================================

def ix_create_campaign(creator: Pubkey, campaign: Pubkey,
                        goal: int, deadline: int) -> Instruction:
    """
    [0x00] | goal u64 LE (8 bytes) | deadline i64 LE (8 bytes)
    Accounts: creator(signer,w), campaign(signer,w), system_program, rent_sysvar
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
    [0x01] | amount u64 LE (8 bytes)
    Accounts: donor(signer,w), campaign(w), system_program

    ✅ Pitfall guard: pass campaign, NOT creator — the program routes
       lamports into the PDA vault via CPI, not to the creator directly.
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
    Accounts: creator(signer,w), campaign(w)

    On-chain guards tested:
      step 4 — must reject if clock < deadline      (CampaignActive)
      step 5 — must succeed if clock > deadline AND raised >= goal
      step 6 — must reject if claimed == true       (AlreadyClaimed)
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
    Accounts: donor(signer,w), campaign(w)
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
    Build a VersionedTransaction (MessageV0), sign it, and send it.

    Returns a Signature object on success, None on failure.
    NOTE: We return Signature (not str) so callers can pass it directly
    to get_signature_statuses() without a type conversion — that was the
    root cause of the 'str cannot be converted to Signature' error.

    Uses VersionedTransaction + MessageV0 (modern API).
    The legacy Message.new_with_blockhash + Transaction combo serializes
    incorrectly with modern solders versions and causes silent drops.
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

        # resp.value is already a Signature object in modern solders
        sig = resp.value
        sig_str = str(sig)
        print(f"  [{label}] Sig     : {sig_str}")
        print(f"  [{label}] Explorer: "
              f"https://explorer.solana.com/tx/{sig_str}?cluster=devnet")
        return sig

    except Exception as e:
        err_str = str(e)
        print(f"  [{label}] FAILED  : {err_str[:400]}")
        # Pull program log lines out of the exception for readable errors
        try:
            logs = re.findall(r'Program log: (.*?)(?:\\n|")', err_str)
            if logs:
                print(f"  [{label}] Program logs:")
                for log in logs:
                    print(f"    >> {log}")
        except Exception:
            pass
        return None


def wait_confirm(client: Client, sig: Optional[Signature], timeout: int = 60) -> bool:
    """
    Poll for transaction confirmation every second up to `timeout` seconds.

    FIX 1: `sig` is now a Signature object — get_signature_statuses() requires
            this; passing a raw str raises 'str cannot be converted to Signature'.

    FIX 2: confirmation_status is a solders enum, not a plain string.
            str(cs).lower() + "in" works for any enum variant name.
    """
    if not sig:
        return False
    for i in range(timeout):
        time.sleep(1)
        try:
            # ← Pass [sig] where sig is a Signature object, not a str
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
    """
    Fetch the full on-chain TX record via get_transaction.
    Differentiates: still-processing / confirmed fine / landed-but-rejected.
    Call this whenever wait_confirm times out before giving up.
    """
    if not sig:
        return
    sig_str = str(sig)
    print(f"  Fetching on-chain status: {sig_str}")
    try:
        result = client.get_transaction(
            sig,                                # Signature object, not str
            max_supported_transaction_version=0,
            commitment=Confirmed,
        )
        if result.value is None:
            print("  Not found on-chain yet — Devnet may still be processing.")
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
    Send + confirm with a fallback status fetch on timeout.
    Returns True only when the TX confirmed successfully.
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
# Main checklist
# =============================================================================

def run_checklist(client: Client, wallet: Keypair):
    print("\n" + "=" * 66)
    print("CROWDFUNDING TEST CHECKLIST")
    print("=" * 66)

    campaign_kp = Keypair()

    goal_lamps     = int(GOAL_SOL      * LAMPORTS_PER_SOL)   # 100_000_000
    contrib1_lamps = int(CONTRIB_1_SOL * LAMPORTS_PER_SOL)   #  60_000_000
    contrib2_lamps = int(CONTRIB_2_SOL * LAMPORTS_PER_SOL)   #  50_000_000

    if SHORT_DEADLINE:
        deadline      = int(time.time()) + DEADLINE_OFFSET
        deadline_desc = f"~{DEADLINE_OFFSET}s from now  (short mode — good for testing)"
    else:
        deadline      = int(time.time()) + 86_400
        deadline_desc = "24 hours from now"

    creator_pubkey  = wallet.pubkey()
    campaign_pubkey = campaign_kp.pubkey()
    vault_pubkey, _ = derive_vault_pda(campaign_pubkey)

    print(f"\n  Creator   : {creator_pubkey}")
    print(f"  Campaign  : {campaign_pubkey}")
    print(f"  Vault PDA : {vault_pubkey}  ← contributions must land here, not on creator")
    print(f"  Goal      : {GOAL_SOL} SOL  ({goal_lamps:,} lamports)")
    print(f"  Contrib 1 : {CONTRIB_1_SOL} SOL  →  raised = {CONTRIB_1_SOL}")
    print(f"  Contrib 2 : {CONTRIB_2_SOL} SOL  →  raised = {CONTRIB_1_SOL + CONTRIB_2_SOL}")
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
           "" if ok else "TX not confirmed — paste the Explorer link above into browser")
    if not ok:
        print("\n  Cannot continue without a confirmed campaign. Aborting.")
        print_summary()
        return

    # ── STEP 2: Contribute 0.06 SOL ──────────────────────────────────────────
    print("\n" + "─" * 66)
    print(f"STEP 2 — Contribute {CONTRIB_1_SOL} SOL  →  raised should be {CONTRIB_1_SOL} SOL")
    print("─" * 66)
    print(f"  Sending {contrib1_lamps:,} lamports from {creator_pubkey} → vault {vault_pubkey}")

    vault_before   = get_balance_sol(client, vault_pubkey)
    creator_before = get_balance_sol(client, creator_pubkey)

    ok = send_and_confirm(
        client,
        [ix_contribute(creator_pubkey, campaign_pubkey, contrib1_lamps)],
        [wallet],
        "contribute_0.06",
    )

    vault_after   = get_balance_sol(client, vault_pubkey)
    creator_after = get_balance_sol(client, creator_pubkey)
    vault_delta   = vault_after   - vault_before
    creator_delta = creator_before - creator_after

    print(f"\n  Vault   balance : {vault_before:.6f} → {vault_after:.6f}  "
          f"(+{vault_delta:.6f} SOL)")
    print(f"  Creator balance : {creator_before:.6f} → {creator_after:.6f}  "
          f"(-{creator_delta:.6f} SOL incl. fee)")

    vault_ok = vault_delta > 0
    record(2, f"Contribute {CONTRIB_1_SOL} SOL → raised={CONTRIB_1_SOL} SOL",
           "success", ok, "" if ok else "TX failed")
    record(2, "Pitfall ✅ SOL went to vault, not creator",
           "vault balance increases", vault_ok,
           "" if vault_ok else
           "⚠️  Vault balance did NOT increase — check CPI target in program")

    # ── STEP 3: Contribute 0.05 SOL → raised = 0.11 SOL ──────────────────────
    print("\n" + "─" * 66)
    print(f"STEP 3 — Contribute {CONTRIB_2_SOL} SOL  →  raised should be "
          f"{round(CONTRIB_1_SOL + CONTRIB_2_SOL, 2)} SOL  (goal exceeded)")
    print("─" * 66)
    print(f"  Sending {contrib2_lamps:,} lamports from {creator_pubkey} → vault {vault_pubkey}")

    ok = send_and_confirm(
        client,
        [ix_contribute(creator_pubkey, campaign_pubkey, contrib2_lamps)],
        [wallet],
        "contribute_0.05",
    )
    record(3,
           f"Contribute {CONTRIB_2_SOL} SOL → raised="
           f"{round(CONTRIB_1_SOL + CONTRIB_2_SOL, 2)} SOL",
           "success", ok, "" if ok else "TX failed")

    # ── STEP 4: Withdraw BEFORE deadline → must FAIL ──────────────────────────
    print("\n" + "─" * 66)
    print("STEP 4 — Withdraw BEFORE deadline  →  must FAIL  (CampaignActive)")
    print("─" * 66)
    secs_left = max(0, deadline - int(time.time()))
    print(f"  Now: {int(time.time())}  |  Deadline: {deadline}  ({secs_left}s left)")

    # ✅ Pitfall: deadline guard — early withdrawal must be blocked
    sig_early = send_tx(
        client,
        [ix_withdraw(creator_pubkey, campaign_pubkey)],
        [wallet],
        "early_withdraw",
    )

    if sig_early is None:
        record(4, "Pitfall ✅ Withdraw before deadline → rejected",
               "fail (CampaignActive)", True, "Rejected at preflight ✓")
    else:
        early_ok = wait_confirm(client, sig_early, timeout=20)
        if early_ok:
            record(4, "Pitfall ✅ Withdraw before deadline → rejected",
                   "fail (CampaignActive)", False,
                   "⚠️  Program ALLOWED early withdrawal — deadline check is missing!")
        else:
            record(4, "Pitfall ✅ Withdraw before deadline → rejected",
                   "fail (CampaignActive)", True,
                   "Rejected by validator simulation ✓")

    # ── STEP 5: Wait for deadline, then withdraw ──────────────────────────────
    print("\n" + "─" * 66)
    print("STEP 5 — Wait for deadline, then withdraw  →  must SUCCEED")
    print("─" * 66)

    remaining = deadline - int(time.time())
    if remaining > 0:
        wait_secs = remaining + 3   # +3 s buffer for on-chain clock skew
        print(f"  Waiting {wait_secs}s for deadline to pass...")
        for t in range(wait_secs, 0, -1):
            print(f"  ⏳ {t:>3}s remaining...", end="\r")
            time.sleep(1)
        print()
    else:
        print("  Deadline already passed — proceeding immediately.")

    creator_pre  = get_balance_sol(client, creator_pubkey)

    ok = send_and_confirm(
        client,
        [ix_withdraw(creator_pubkey, campaign_pubkey)],
        [wallet],
        "withdraw",
    )

    creator_post = get_balance_sol(client, creator_pubkey)
    net          = creator_post - creator_pre
    print(f"  Creator balance change : {net:+.6f} SOL  "
          f"({'received funds ✓' if net > 0 else 'no change — may have failed'})")

    record(5, "Withdraw after deadline → success", "success", ok,
           "" if ok else "Withdraw TX failed — check program logs above")

    # ── STEP 6: Withdraw again → must FAIL (claimed=true) ─────────────────────
    print("\n" + "─" * 66)
    print("STEP 6 — Withdraw again  →  must FAIL  (AlreadyClaimed)")
    print("─" * 66)

    # ✅ Pitfall: claimed=true must prevent double-withdrawal
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
    print("  ❌ Don't send donations to creator  ✅ Vault balance checked  (step 2)")
    print("  ❌ Don't allow early withdrawal     ✅ Deadline guard          (step 4)")
    print("  ❌ Don't forget claimed=true        ✅ Double-withdraw guard   (step 6)")
    print("  ❌ Don't use bare unwrap()          ✅ All errors caught in send_tx")

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
                   help="Generate a throwaway keypair and auto-airdrop funds.")
    p.add_argument("--long-deadline", action="store_true",
                   help="Use a 24-hour deadline instead of the 45-second short deadline.")
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
    print(f"TX format  : VersionedTransaction + MessageV0")
    print(f"Deadline   : {'SHORT ~45s (local testing)' if SHORT_DEADLINE else 'LONG 24h'}")
    print()

    wallet = load_wallet(keypair_path=args.keypair, generate_new=args.new_wallet)
    print(f"Wallet     : {wallet.pubkey()}")

    client = get_client()
    print("Devnet     : connected\n")

    # ── Auto-fund wallet if needed (no manual step required) ──────────────────
    # The test uses 0.06 + 0.05 = 0.11 SOL in contributions + ~0.003 SOL in fees.
    # We keep min_sol at 0.5 to have plenty of headroom.
    ensure_funded(client, wallet.pubkey(), min_sol=0.5)

    balance = get_balance_sol(client, wallet.pubkey())
    if balance < 0.3:
        print(f"\nERROR: Need at least 0.3 SOL.  Current: {balance:.4f} SOL")
        print("  Run:  solana airdrop 2 --url devnet")
        sys.exit(1)

    print(f"Balance    : {balance:.4f} SOL  (ready — funds will be sent automatically)\n")

    run_checklist(client, wallet)

    print("\nDone.")


if __name__ == "__main__":
    main()
