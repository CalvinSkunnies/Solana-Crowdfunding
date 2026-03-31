use borsh::{BorshDeserialize, BorshSerialize};
use solana_program::{
    account_info::{next_account_info, AccountInfo},
    clock::Clock,
    entrypoint::ProgramResult,
    msg,
    program::invoke,
    program::invoke_signed,
    program_error::ProgramError,
    pubkey::Pubkey,
    rent::Rent,
    system_instruction,
    sysvar::Sysvar,
};

// ─────────────────────────────────────────────────────────────────────────────
// Data structures
// ─────────────────────────────────────────────────────────────────────────────

/// Campaign state stored in the campaign account.
///
/// Borsh layout (58 bytes):
///   creator   Pubkey   32
///   goal      u64       8
///   raised    u64       8
///   deadline  i64       8
///   claimed   bool      1
///   bump      u8        1
///                      ──
///                      58
#[derive(BorshSerialize, BorshDeserialize, Debug, Clone)]
pub struct Campaign {
    pub creator:  Pubkey,
    pub goal:     u64,
    pub raised:   u64,
    pub deadline: i64,
    pub claimed:  bool,
    pub bump:     u8,   // vault PDA bump seed — stored so withdraw/refund never re-derive
}

impl Campaign {
    /// Exact serialised size (no Anchor discriminator — native program).
    pub const LEN: usize = 32 + 8 + 8 + 8 + 1 + 1; // = 58
}

// ─────────────────────────────────────────────────────────────────────────────
// Error codes
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CrowdfundingError {
    DeadlineInPast    = 0,
    CampaignEnded     = 1,
    GoalNotReached    = 2,
    GoalReached       = 3,
    AlreadyClaimed    = 4,
    NotCreator        = 5,
    CampaignActive    = 6,
    InvalidAccount    = 7,
    InsufficientFunds = 8,
}

impl From<CrowdfundingError> for ProgramError {
    fn from(e: CrowdfundingError) -> Self {
        ProgramError::Custom(e as u32)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Program ID — update this after every fresh deployment
// ─────────────────────────────────────────────────────────────────────────────

solana_program::declare_id!("DKsRhfniEEv3EcNgvbid11aDAAC3Mbsxui3rTQnU5GS3");

// ─────────────────────────────────────────────────────────────────────────────
// Entrypoint
// ─────────────────────────────────────────────────────────────────────────────

solana_program::entrypoint!(process_instruction);

pub fn process_instruction(
    program_id:       &Pubkey,
    accounts:         &[AccountInfo],
    instruction_data: &[u8],
) -> ProgramResult {
    if instruction_data.is_empty() {
        return Err(ProgramError::InvalidInstructionData);
    }

    match instruction_data[0] {
        0 => {
            if instruction_data.len() < 17 {
                return Err(ProgramError::InvalidInstructionData);
            }
            let goal     = u64::from_le_bytes(instruction_data[1..9].try_into().unwrap());
            let deadline = i64::from_le_bytes(instruction_data[9..17].try_into().unwrap());
            create_campaign(program_id, accounts, goal, deadline)
        }
        1 => {
            if instruction_data.len() < 9 {
                return Err(ProgramError::InvalidInstructionData);
            }
            let amount = u64::from_le_bytes(instruction_data[1..9].try_into().unwrap());
            contribute(program_id, accounts, amount)
        }
        2 => withdraw(program_id, accounts),
        3 => {
            if instruction_data.len() < 9 {
                return Err(ProgramError::InvalidInstructionData);
            }
            let amount = u64::from_le_bytes(instruction_data[1..9].try_into().unwrap());
            refund(program_id, accounts, amount)
        }
        _ => Err(ProgramError::InvalidInstructionData),
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Helper — derive vault PDA
// Seeds: [b"vault", campaign_pubkey]
// ─────────────────────────────────────────────────────────────────────────────

fn find_vault_pda(campaign_key: &Pubkey, program_id: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(
        &[b"vault", campaign_key.as_ref()],
        program_id,
    )
}

// ─────────────────────────────────────────────────────────────────────────────
// Instruction 0 — CreateCampaign
//
// Account layout:
//   0  creator          signer, writable   pays all rent
//   1  campaign         signer, writable   new account — owned by this program
//   2  vault            writable           PDA ["vault", campaign] — new account
//   3  system_program
//   4  rent_sysvar
// ─────────────────────────────────────────────────────────────────────────────

fn create_campaign(
    program_id: &Pubkey,
    accounts:   &[AccountInfo],
    goal:       u64,
    deadline:   i64,
) -> ProgramResult {
    let iter = &mut accounts.iter();

    let creator          = next_account_info(iter)?;
    let campaign_account = next_account_info(iter)?;
    let vault_account    = next_account_info(iter)?;
    let system_program   = next_account_info(iter)?;
    let rent_sysvar      = next_account_info(iter)?;

    if !creator.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }

    // Deadline must be in the future
    let clock = Clock::get()?;
    if deadline <= clock.unix_timestamp {
        msg!("Error: Deadline must be in the future");
        return Err(CrowdfundingError::DeadlineInPast.into());
    }

    // Derive vault PDA and verify caller supplied the correct address
    let (vault_pda, bump) = find_vault_pda(campaign_account.key, program_id);
    if vault_pda != *vault_account.key {
        msg!("Error: Vault PDA mismatch");
        return Err(CrowdfundingError::InvalidAccount.into());
    }

    let rent = Rent::from_account_info(rent_sysvar)?;

    // ── Create campaign account (program-owned, stores Campaign state) ────────
    invoke(
        &system_instruction::create_account(
            creator.key,
            campaign_account.key,
            rent.minimum_balance(Campaign::LEN),
            Campaign::LEN as u64,
            program_id,
        ),
        &[creator.clone(), campaign_account.clone(), system_program.clone()],
    )?;

    // ── Create vault account (system-owned PDA, stores SOL donations) ─────────
    // Zero data — it only holds lamports.  The program signs for it via PDA.
    invoke_signed(
        &system_instruction::create_account(
            creator.key,
            vault_account.key,
            rent.minimum_balance(0),
            0,
            &solana_program::system_program::id(),
        ),
        &[creator.clone(), vault_account.clone(), system_program.clone()],
        &[&[b"vault", campaign_account.key.as_ref(), &[bump]]],
    )?;

    // ── Persist campaign state ────────────────────────────────────────────────
    let campaign = Campaign {
        creator:  *creator.key,
        goal,
        raised:   0,
        deadline,
        claimed:  false,
        bump,
    };
    campaign.serialize(&mut &mut campaign_account.data.borrow_mut()[..])?;

    msg!("Campaign created: goal={}, deadline={}", goal, deadline);
    msg!("Vault PDA: {}", vault_pda);

    Ok(())
}

// ─────────────────────────────────────────────────────────────────────────────
// Instruction 1 — Contribute
//
// SOL flows: donor → vault PDA (NOT into the campaign account).
//
// Account layout:
//   0  donor            signer, writable
//   1  campaign         writable           raised counter update
//   2  vault            writable           PDA — receives the SOL
//   3  system_program
// ─────────────────────────────────────────────────────────────────────────────

fn contribute(
    program_id: &Pubkey,
    accounts:   &[AccountInfo],
    amount:     u64,
) -> ProgramResult {
    let iter = &mut accounts.iter();

    let donor            = next_account_info(iter)?;
    let campaign_account = next_account_info(iter)?;
    let vault_account    = next_account_info(iter)?;
    let system_program   = next_account_info(iter)?;

    if !donor.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }

    // Verify vault PDA
    let (vault_pda, _) = find_vault_pda(campaign_account.key, program_id);
    if vault_pda != *vault_account.key {
        msg!("Error: Vault PDA mismatch");
        return Err(CrowdfundingError::InvalidAccount.into());
    }

    let mut campaign = Campaign::try_from_slice(&campaign_account.data.borrow())?;

    // Campaign must still be active
    let clock = Clock::get()?;
    if clock.unix_timestamp >= campaign.deadline {
        msg!("Error: Campaign has ended");
        return Err(CrowdfundingError::CampaignEnded.into());
    }

    // Transfer SOL from donor → vault PDA
    invoke(
        &system_instruction::transfer(donor.key, vault_account.key, amount),
        &[donor.clone(), vault_account.clone(), system_program.clone()],
    )?;

    // Update the raised counter
    campaign.raised = campaign
        .raised
        .checked_add(amount)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    campaign.serialize(&mut &mut campaign_account.data.borrow_mut()[..])?;

    msg!("Contributed: {} lamports, total={}", amount, campaign.raised);

    Ok(())
}

// ─────────────────────────────────────────────────────────────────────────────
// Instruction 2 — Withdraw
//
// Creator claims ALL vault funds after:
//   • deadline has passed
//   • raised >= goal
//   • not already claimed
//
// Uses invoke_signed to transfer from vault PDA → creator.
//
// Account layout:
//   0  creator          signer, writable   receives all vault SOL
//   1  campaign         writable           claimed = true
//   2  vault            writable           PDA — sends SOL
//   3  system_program
// ─────────────────────────────────────────────────────────────────────────────

fn withdraw(
    program_id: &Pubkey,
    accounts:   &[AccountInfo],
) -> ProgramResult {
    let iter = &mut accounts.iter();

    let creator          = next_account_info(iter)?;
    let campaign_account = next_account_info(iter)?;
    let vault_account    = next_account_info(iter)?;
    let system_program   = next_account_info(iter)?;

    if !creator.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }

    let mut campaign = Campaign::try_from_slice(&campaign_account.data.borrow())?;

    // Verify vault PDA (use stored bump so we never re-derive from scratch)
    let (vault_pda, _) = find_vault_pda(campaign_account.key, program_id);
    if vault_pda != *vault_account.key {
        msg!("Error: Vault PDA mismatch");
        return Err(CrowdfundingError::InvalidAccount.into());
    }
    let bump = campaign.bump;

    // Auth + state checks
    if campaign.creator != *creator.key {
        msg!("Error: Not the campaign creator");
        return Err(CrowdfundingError::NotCreator.into());
    }
    let clock = Clock::get()?;
    if clock.unix_timestamp < campaign.deadline {
        msg!("Error: Campaign still active");
        return Err(CrowdfundingError::CampaignActive.into());
    }
    if campaign.raised < campaign.goal {
        msg!("Error: Goal not reached");
        return Err(CrowdfundingError::GoalNotReached.into());
    }
    if campaign.claimed {
        msg!("Error: Already claimed");
        return Err(CrowdfundingError::AlreadyClaimed.into());
    }

    // Drain vault → creator
    let vault_lamports = **vault_account.lamports.borrow();
    if vault_lamports == 0 {
        msg!("Error: Vault is empty");
        return Err(CrowdfundingError::InsufficientFunds.into());
    }

    invoke_signed(
        &system_instruction::transfer(vault_account.key, creator.key, vault_lamports),
        &[vault_account.clone(), creator.clone(), system_program.clone()],
        &[&[b"vault", campaign_account.key.as_ref(), &[bump]]],
    )?;

    // Mark claimed — prevents double-withdrawal
    campaign.claimed = true;
    campaign.raised  = 0;
    campaign.serialize(&mut &mut campaign_account.data.borrow_mut()[..])?;

    msg!("Withdrawn: {} lamports to creator", vault_lamports);

    Ok(())
}

// ─────────────────────────────────────────────────────────────────────────────
// Instruction 3 — Refund
//
// Donor reclaims their contribution when:
//   • deadline has passed
//   • raised < goal
//
// Uses invoke_signed to transfer from vault PDA → donor.
//
// Account layout:
//   0  donor            signer, writable   receives refund
//   1  campaign         writable
//   2  vault            writable           PDA — sends SOL
//   3  system_program
// ─────────────────────────────────────────────────────────────────────────────

fn refund(
    program_id: &Pubkey,
    accounts:   &[AccountInfo],
    amount:     u64,
) -> ProgramResult {
    let iter = &mut accounts.iter();

    let donor            = next_account_info(iter)?;
    let campaign_account = next_account_info(iter)?;
    let vault_account    = next_account_info(iter)?;
    let system_program   = next_account_info(iter)?;

    if !donor.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }

    let campaign = Campaign::try_from_slice(&campaign_account.data.borrow())?;

    // Verify vault PDA
    let (vault_pda, _) = find_vault_pda(campaign_account.key, program_id);
    if vault_pda != *vault_account.key {
        msg!("Error: Vault PDA mismatch");
        return Err(CrowdfundingError::InvalidAccount.into());
    }
    let bump = campaign.bump;

    // State checks
    let clock = Clock::get()?;
    if clock.unix_timestamp < campaign.deadline {
        msg!("Error: Campaign still active");
        return Err(CrowdfundingError::CampaignActive.into());
    }
    if campaign.raised >= campaign.goal {
        msg!("Error: Goal reached — cannot refund");
        return Err(CrowdfundingError::GoalReached.into());
    }

    let vault_lamports = **vault_account.lamports.borrow();
    if vault_lamports < amount {
        msg!("Error: Insufficient funds in vault");
        return Err(CrowdfundingError::InsufficientFunds.into());
    }

    // Refund donor from vault using invoke_signed
    invoke_signed(
        &system_instruction::transfer(vault_account.key, donor.key, amount),
        &[vault_account.clone(), donor.clone(), system_program.clone()],
        &[&[b"vault", campaign_account.key.as_ref(), &[bump]]],
    )?;

    msg!("Refunded: {} lamports to {}", amount, donor.key);

    Ok(())
}
