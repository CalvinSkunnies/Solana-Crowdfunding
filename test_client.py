"""
Solana Crowdfunding Platform - Python Test Client

This client demonstrates how to interact with the crowdfunding smart contract.

Program ID: CrwdN8ruKmWC3uxRWD9LU1RqoT4V3WQ3iRCJ5rRDxS3q

Usage:
1. pip install solders solana
2. python test_client.py

Note: You need SOL in your wallet to run tests on devnet.
"""

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction, TransactionInstruction
from solders import system_program
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
import base64
import time

# Program ID
PROGRAM_ID = Pubkey.from_string("CrwdN8ruKmWC3uxRWD9LU1RqoT4V3WQ3iRCJ5rRDxS3q")

# RPC endpoint
RPC_URL = "https://api.devnet.solana.com"
client = Client(RPC_URL)

# Load keypair from file or generate new one
def get_keypair():
    try:
        with open('/root/.config/solana/id.json', 'r') as f:
            import json
            keypair_data = json.load(f)
            # First 32 bytes are the secret key
            secret_bytes = bytes(keypair_data[:32])
            return Keypair.from_bytes(secret_bytes)
    except:
        print("No keypair found, generating new test keypair...")
        kp = Keypair()
        print(f"Public key: {kp.pubkey()}")
        return kp

# Create campaign instruction
def create_campaign_ix(creator: Pubkey, campaign: Pubkey, goal: int, deadline: int) -> TransactionInstruction:
    data = bytearray()
    data.append(0)  # Instruction: Create Campaign
    data.extend(goal.to_bytes(8, 'little'))  # goal (u64)
    data.extend(deadline.to_bytes(8, 'little', signed=True))  # deadline (i64)
    
    return TransactionInstruction(
        keys=[
            system_program.AccountMeta(creator, True, True),  # signer
            system_program.AccountMeta(campaign, False, True),  # campaign account
            system_program.AccountMeta(Pubkey.from_string("SysvarRent111111111111111111111111111111111"), False, False),  # rent
            system_program.AccountMeta(Pubkey.from_string("KeccakSecp256k1SEcw凹FMAC5KFoxmCZAPK"), False, False),  # secp256k1 (for signature)
        ],
        program_id=PROGRAM_ID,
        data=bytes(data)
    )

# Contribute instruction
def contribute_ix(donor: Pubkey, campaign: Pubkey, amount: int) -> TransactionInstruction:
    data = bytearray()
    data.append(1)  # Instruction: Contribute
    data.extend(amount.to_bytes(8, 'little'))
    
    return TransactionInstruction(
        keys=[
            system_program.AccountMeta(donor, True, True),
            system_program.AccountMeta(campaign, False, True),
        ],
        program_id=PROGRAM_ID,
        data=bytes(data)
    )

# Withdraw instruction
def withdraw_ix(creator: Pubkey, campaign: Pubkey) -> TransactionInstruction:
    data = bytearray()
    data.append(2)  # Instruction: Withdraw
    
    return TransactionInstruction(
        keys=[
            system_program.AccountMeta(creator, True, True),
            system_program.AccountMeta(campaign, False, True),
        ],
        program_id=PROGRAM_ID,
        data=bytes(data)
    )

# Refund instruction
def refund_ix(donor: Pubkey, campaign: Pubkey, amount: int) -> TransactionInstruction:
    data = bytearray()
    data.append(3)  # Instruction: Refund
    data.extend(amount.to_bytes(8, 'little'))
    
    return TransactionInstruction(
        keys=[
            system_program.AccountMeta(donor, True, True),
            system_program.AccountMeta(campaign, False, True),
        ],
        program_id=PROGRAM_ID,
        data=bytes(data)
    )

def run_tests():
    wallet = get_keypair()
    print(f"Wallet: {wallet.pubkey()}")
    
    # Check balance
    balance = client.get_balance(wallet.pubkey(), commitment=Confirmed).value
    print(f"Balance: {balance / 1e9:.2} SOL")
    
    if balance < 1e9:
        print("Insufficient balance. Please airdrop SOL first:")
        print("  solana airdrop 2")
        return
    
    # Test 1: Create Campaign
    print("\n=== Test 1: Create Campaign ===")
    campaign = Keypair.generate()
    goal = 1000 * 1e9  # 1000 SOL
    deadline = int(time.time()) + 86400  # 24 hours from now
    
    # Get rent exemption
    rent = client.get_minimum_balance_for_rent_exemption(200).value
    
    # Create transaction
    txn = Transaction()
    txn.add(
        system_program.create_account(
            from_pubkey=wallet.pubkey(),
            to_pubkey=campaign.pubkey(),
            lamports=rent,
            space=200,
            program_id=PROGRAM_ID
        )
    )
    txn.add(create_campaign_ix(wallet.pubkey(), campaign.pubkey(), goal, deadline))
    
    try:
        sig = client.send_transaction(txn, wallet, campaign, commitment=Confirmed).value
        print(f"Campaign created! Signature: {sig}")
        print(f"Campaign address: {campaign.pubkey()}")
    except Exception as e:
        print(f"Error creating campaign: {e}")
        return
    
    # Wait for confirmation
    time.sleep(2)
    
    # Test 2: Contribute
    print("\n=== Test 2: Contribute 600 SOL ===")
    txn = Transaction()
    txn.add(contribute_ix(wallet.pubkey(), campaign.pubkey(), 600 * 1e9))
    
    try:
        sig = client.send_transaction(txn, wallet, commitment=Confirmed).value
        print(f"Contributed! Signature: {sig}")
    except Exception as e:
        print(f"Error contributing: {e}")
    
    time.sleep(2)
    
    # Test 3: Contribute more
    print("\n=== Test 3: Contribute 500 SOL ===")
    txn = Transaction()
    txn.add(contribute_ix(wallet.pubkey(), campaign.pubkey(), 500 * 1e9))
    
    try:
        sig = client.send_transaction(txn, wallet, commitment=Confirmed).value
        print(f"Contributed! Signature: {sig}")
    except Exception as e:
        print(f"Error contributing: {e}")
    
    print("\n=== Tests Complete ===")
    print(f"Campaign: {campaign.pubkey()}")
    print(f"Goal: {goal / 1e9} SOL")
    print(f"Deadline: {deadline} (in ~24 hours)")
    print("\nNote: Withdraw and Refund tests require waiting for deadline")

if __name__ == "__main__":
    run_tests()