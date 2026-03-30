/**
 * Solana Crowdfunding Platform - Test Client
 * 
 * This client demonstrates how to interact with the crowdfunding smart contract.
 * 
 * Program ID: CrwdN8ruKmWC3uxRWD9LU1RqoT4V3WQ3iRCJ5rRDxS3q
 * 
 * Instructions:
 * 1. Install dependencies: npm install @solana/web3.js @solana/anchor
 * 2. Update the program ID if different
 * 3. Run with: npx ts-node test-client.ts
 */

import {
  Connection,
  PublicKey,
  Transaction,
  SystemProgram,
  Keypair,
  sendAndConfirmTransaction,
} from "@solana/web3.js";
import * as Buffer from "buffer";

// Program ID
const PROGRAM_ID = new PublicKey("3Dc6ZJsWiQm6CmDUt5MY4izbdLgpBU2KbhfSmqpVcayM");

// Connection to devnet
const connection = new Connection("https://api.devnet.solana.com", "confirmed");

// Helper function to create instruction data
function createInstructionData(instruction: number, data: Buffer): Buffer {
  const buf = Buffer.alloc(1 + data.length);
  buf.writeUInt8(instruction, 0);
  data.copy(buf, 1);
  return buf;
}

// 1. CREATE CAMPAIGN
// Instruction: 0
// Data: goal (u64) + deadline (i64) = 16 bytes
async function createCampaign(
  creator: Keypair,
  goal: number,       // in lamports
  deadline: number    // Unix timestamp
): Promise<Transaction> {
  const campaignAccount = Keypair.generate();
  
  const goalBuffer = Buffer.alloc(8);
  goalBuffer.writeBigUInt64LE(BigInt(goal), 0);
  
  const deadlineBuffer = Buffer.alloc(8);
  deadlineBuffer.writeBigInt64LE(BigInt(deadline), 0);
  
  const data = Buffer.concat([goalBuffer, deadlineBuffer]);
  const instructionData = createInstructionData(0, data);
  
  const transaction = new Transaction();
  
  // Create campaign account
  const rent = await connection.getMinimumBalanceForRentExemption(1000);
  
  transaction.add(
    SystemProgram.createAccount({
      fromPubkey: creator.publicKey,
      newAccountPubkey: campaignAccount.publicKey,
      lamports: rent,
      space: 1000,
      programId: PROGRAM_ID,
    }),
    new TransactionInstruction({
      keys: [
        { pubkey: creator.publicKey, isSigner: true, isWritable: true },
        { pubkey: campaignAccount.publicKey, isSigner: false, isWritable: true },
        { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
      ],
      programId: PROGRAM_ID,
      data: instructionData,
    })
  );
  
  return transaction;
}

// 2. CONTRIBUTE
// Instruction: 1
// Data: amount (u64) = 8 bytes
async function contribute(
  donor: Keypair,
  campaignPubkey: PublicKey,
  amount: number  // in lamports
): Promise<Transaction> {
  const amountBuffer = Buffer.alloc(8);
  amountBuffer.writeBigUInt64LE(BigInt(amount), 0);
  
  const instructionData = createInstructionData(1, amountBuffer);
  
  const transaction = new Transaction();
  
  transaction.add(
    new TransactionInstruction({
      keys: [
        { pubkey: donor.publicKey, isSigner: true, isWritable: true },
        { pubkey: campaignPubkey, isSigner: false, isWritable: true },
        { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
      ],
      programId: PROGRAM_ID,
      data: instructionData,
    })
  );
  
  return transaction;
}

// 3. WITHDRAW (Creator only, after deadline, goal reached)
// Instruction: 2
// Data: none
async function withdraw(
  creator: Keypair,
  campaignPubkey: PublicKey
): Promise<Transaction> {
  const instructionData = createInstructionData(2, Buffer.alloc(0));
  
  const transaction = new Transaction();
  
  transaction.add(
    new TransactionInstruction({
      keys: [
        { pubkey: creator.publicKey, isSigner: true, isWritable: true },
        { pubkey: campaignPubkey, isSigner: false, isWritable: true },
      ],
      programId: PROGRAM_ID,
      data: instructionData,
    })
  );
  
  return transaction;
}

// 4. REFUND (Donor only, after deadline, goal NOT reached)
// Instruction: 3
// Data: amount (u64) = 8 bytes
async function refund(
  donor: Keypair,
  campaignPubkey: PublicKey,
  amount: number  // in lamports
): Promise<Transaction> {
  const amountBuffer = Buffer.alloc(8);
  amountBuffer.writeBigUInt64LE(BigInt(amount), 0);
  
  const instructionData = createInstructionData(3, amountBuffer);
  
  const transaction = new Transaction();
  
  transaction.add(
    new TransactionInstruction({
      keys: [
        { pubkey: donor.publicKey, isSigner: true, isWritable: true },
        { pubkey: campaignPubkey, isSigner: false, isWritable: true },
      ],
      programId: PROGRAM_ID,
      data: instructionData,
    })
  );
  
  return transaction;
}

// Test Scenario
async function runTest() {
  // Use your keypair (from ~/.config/solana/id.json or Phantom wallet)
  // For testing, generate a new keypair:
  const wallet = Keypair.generate();
  
  console.log("Test Wallet:", wallet.publicKey.toBase58());
  
  // Request airdrop (if on devnet)
  console.log("\nRequesting airdrop...");
  const airdropSignature = await connection.requestAirdrop(
    wallet.publicKey,
    5 * 1e9  // 5 SOL
  );
  await connection.confirmTransaction(airdropSignature);
  console.log("Airdrop received!");
  
  // Test 1: Create Campaign
  console.log("\n=== Test 1: Create Campaign ===");
  const goal = 1000 * 1e9;  // 1000 SOL in lamports
  const deadline = Math.floor(Date.now() / 1000) + 86400;  // Tomorrow
  
  const createTx = await createCampaign(wallet, goal, deadline);
  const createSig = await sendAndConfirmTransaction(connection, createTx, [wallet]);
  console.log("Campaign created! Signature:", createSig);
  
  // Test 2: Contribute
  console.log("\n=== Test 2: Contribute ===");
  const campaignPubkey = /* campaign account address from createTx */;
  const contributeTx = await contribute(wallet, campaignPubkey, 600 * 1e9);
  const contributeSig = await sendAndConfirmTransaction(connection, contributeTx, [wallet]);
  console.log("Contributed 600 SOL! Signature:", contributeSig);
  
  // Test 3: Contribute more
  console.log("\n=== Test 3: Contribute More ===");
  const contributeTx2 = await contribute(wallet, campaignPubkey, 500 * 1e9);
  const contributeSig2 = await sendAndConfirmTransaction(connection, contributeTx2, [wallet]);
  console.log("Contributed 500 more SOL! Signature:", contributeSig2);
  
  console.log("\n=== Tests Complete ===");
  console.log("Note: Withdraw and Refund tests require waiting for deadline to pass");
}

// Export functions for use in other files
export {
  createCampaign,
  contribute,
  withdraw,
  refund,
  PROGRAM_ID,
};

// Run if executed directly
runTest().catch(console.error);
