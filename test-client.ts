/**
 * Solana Crowdfunding — TypeScript Test Client
 * PDA Vault + Contribution Tracking Edition
 *
 * Usage:
 *   npm install @solana/web3.js
 *   npx ts-node test-client.ts
 */

import {
  Connection, PublicKey, TransactionInstruction, TransactionMessage,
  VersionedTransaction, SystemProgram, Keypair, LAMPORTS_PER_SOL,
} from "@solana/web3.js";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

const PROGRAM_ID = new PublicKey("DKsRhfniEEv3EcNgvbid11aDAAC3Mbsxui3rTQnU5GS3");
const connection = new Connection("https://api.devnet.solana.com", "confirmed");
const DEADLINE_SECS = 45;

function findVaultPDA(campaign: PublicKey): [PublicKey, number] {
  return PublicKey.findProgramAddressSync([Buffer.from("vault"), campaign.toBuffer()], PROGRAM_ID);
}
function findContributionPDA(campaign: PublicKey, donor: PublicKey): [PublicKey, number] {
  return PublicKey.findProgramAddressSync(
    [Buffer.from("contribution"), campaign.toBuffer(), donor.toBuffer()], PROGRAM_ID);
}

function loadWallet(): Keypair {
  const paths = [
    process.env.SOLANA_KEYPAIR_PATH,
    path.join(os.homedir(), ".config", "solana", "id.json"),
    path.join(os.homedir(), "solana", "id.json"),
    "id.json",
  ].filter(Boolean) as string[];
  for (const p of paths) {
    if (fs.existsSync(p)) {
      console.log("Using keypair:", p);
      return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(fs.readFileSync(p, "utf-8"))));
    }
  }
  console.log("No keypair found — generating throwaway.");
  const kp = Keypair.generate();
  console.log("  Pubkey:", kp.publicKey.toBase58());
  return kp;
}

/*
 * Account layouts (must match lib.rs next_account_info order):
 *
 * CreateCampaign: creator(sw), campaign(sw), vault(w), system_program
 * Contribute:     donor(sw), campaign(w), vault(w), contribution(w), system_program
 * Withdraw:       creator(sw), campaign(w), vault(w), system_program
 * Refund:         donor(sw), campaign(w), vault(w), contribution(w), system_program
 */

function ixCreateCampaign(
  creator: PublicKey, campaign: PublicKey, vault: PublicKey,
  goal: bigint, deadline: bigint,
): TransactionInstruction {
  const data = Buffer.alloc(17);
  data.writeUInt8(0, 0);
  data.writeBigUInt64LE(goal, 1);
  data.writeBigInt64LE(deadline, 9);
  return new TransactionInstruction({
    programId: PROGRAM_ID,
    keys: [
      { pubkey: creator,  isSigner: true,  isWritable: true },
      { pubkey: campaign, isSigner: true,  isWritable: true },
      { pubkey: vault,    isSigner: false, isWritable: true },
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
    ],
    data,
  });
}

function ixContribute(
  donor: PublicKey, campaign: PublicKey, vault: PublicKey,
  contribution: PublicKey, amount: bigint,
): TransactionInstruction {
  const data = Buffer.alloc(9);
  data.writeUInt8(1, 0);
  data.writeBigUInt64LE(amount, 1);
  return new TransactionInstruction({
    programId: PROGRAM_ID,
    keys: [
      { pubkey: donor,        isSigner: true,  isWritable: true },
      { pubkey: campaign,     isSigner: false, isWritable: true },
      { pubkey: vault,        isSigner: false, isWritable: true },
      { pubkey: contribution, isSigner: false, isWritable: true },
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
    ],
    data,
  });
}

function ixWithdraw(
  creator: PublicKey, campaign: PublicKey, vault: PublicKey,
): TransactionInstruction {
  return new TransactionInstruction({
    programId: PROGRAM_ID,
    keys: [
      { pubkey: creator,  isSigner: true,  isWritable: true },
      { pubkey: campaign, isSigner: false, isWritable: true },
      { pubkey: vault,    isSigner: false, isWritable: true },
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
    ],
    data: Buffer.from([2]),
  });
}

function ixRefund(
  donor: PublicKey, campaign: PublicKey, vault: PublicKey,
  contribution: PublicKey,
): TransactionInstruction {
  return new TransactionInstruction({
    programId: PROGRAM_ID,
    keys: [
      { pubkey: donor,        isSigner: true,  isWritable: true },
      { pubkey: campaign,     isSigner: false, isWritable: true },
      { pubkey: vault,        isSigner: false, isWritable: true },
      { pubkey: contribution, isSigner: false, isWritable: true },
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
    ],
    data: Buffer.from([3]),
  });
}

async function sendTx(
  ixs: TransactionInstruction[], signers: Keypair[], label: string,
): Promise<string | null> {
  try {
    const { blockhash } = await connection.getLatestBlockhash("confirmed");
    const msg = new TransactionMessage({
      payerKey: signers[0].publicKey, recentBlockhash: blockhash, instructions: ixs,
    }).compileToV0Message();
    const tx = new VersionedTransaction(msg);
    tx.sign(signers);
    const sig = await connection.sendTransaction(tx, { skipPreflight: false });
    console.log("  [" + label + "] sig:", sig);
    return sig;
  } catch (e: any) {
    console.log("  [" + label + "] FAILED:", String(e).slice(0, 300));
    return null;
  }
}

async function confirmTx(sig: string | null, timeout = 60): Promise<boolean> {
  if (!sig) return false;
  const start = Date.now();
  while (Date.now() - start < timeout * 1000) {
    await new Promise(r => setTimeout(r, 1000));
    const { value } = await connection.getSignatureStatuses([sig]);
    if (value[0]?.confirmationStatus === "confirmed" || value[0]?.confirmationStatus === "finalized")
      return true;
  }
  return false;
}

function sleep(ms: number) { return new Promise(r => setTimeout(r, ms)); }

async function runTest() {
  console.log("Solana Crowdfunding — TS Test [PDA Vault + Contribution Tracking]");
  const wallet = loadWallet();
  console.log("Wallet:", wallet.publicKey.toBase58());

  const balance = await connection.getBalance(wallet.publicKey);
  console.log("Balance:", balance / LAMPORTS_PER_SOL, "SOL");
  if (balance < 0.5 * LAMPORTS_PER_SOL) {
    console.log("Requesting airdrop...");
    const sig = await connection.requestAirdrop(wallet.publicKey, 2 * LAMPORTS_PER_SOL);
    await connection.confirmTransaction(sig, "confirmed");
    console.log("Airdrop confirmed!");
  }

  const campaignKp = Keypair.generate();
  const [vaultPub] = findVaultPDA(campaignKp.publicKey);
  const [contribPub] = findContributionPDA(campaignKp.publicKey, wallet.publicKey);
  const goal = BigInt(0.1 * LAMPORTS_PER_SOL);
  const deadline = BigInt(Math.floor(Date.now() / 1000) + DEADLINE_SECS);

  console.log("Campaign:", campaignKp.publicKey.toBase58());
  console.log("Vault:", vaultPub.toBase58());
  console.log("Contrib:", contribPub.toBase58());

  // Create campaign
  let sig = await sendTx(
    [ixCreateCampaign(wallet.publicKey, campaignKp.publicKey, vaultPub, goal, deadline)],
    [wallet, campaignKp], "create");
  if (!(await confirmTx(sig))) { console.log("Create FAIL"); return; }

  // Contribute 0.06 + 0.05
  sig = await sendTx(
    [ixContribute(wallet.publicKey, campaignKp.publicKey, vaultPub, contribPub, BigInt(0.06 * LAMPORTS_PER_SOL))],
    [wallet], "contrib_1");
  await confirmTx(sig);

  sig = await sendTx(
    [ixContribute(wallet.publicKey, campaignKp.publicKey, vaultPub, contribPub, BigInt(0.05 * LAMPORTS_PER_SOL))],
    [wallet], "contrib_2");
  await confirmTx(sig);

  // Early withdraw (should fail)
  sig = await sendTx([ixWithdraw(wallet.publicKey, campaignKp.publicKey, vaultPub)], [wallet], "early_wd");
  console.log(!sig ? "  Early withdraw correctly rejected" : "  Unexpected success");

  // Wait for deadline
  const remaining = Number(deadline) - Math.floor(Date.now() / 1000);
  if (remaining > 0) { console.log("Waiting", remaining + 3, "s..."); await sleep((remaining + 3) * 1000); }

  // Withdraw
  sig = await sendTx([ixWithdraw(wallet.publicKey, campaignKp.publicKey, vaultPub)], [wallet], "withdraw");
  console.log(await confirmTx(sig) ? "  Withdraw SUCCESS" : "  Withdraw FAIL");

  // Double withdraw (should fail)
  sig = await sendTx([ixWithdraw(wallet.publicKey, campaignKp.publicKey, vaultPub)], [wallet], "double_wd");
  console.log(!sig ? "  Double withdraw correctly rejected" : "  Unexpected success");

  console.log("\nTests complete!");
}

runTest().catch(console.error);

export { PROGRAM_ID, findVaultPDA, findContributionPDA, ixCreateCampaign, ixContribute, ixWithdraw, ixRefund };
