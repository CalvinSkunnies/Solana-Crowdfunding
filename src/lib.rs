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
    system_program,
    sysvar::Sysvar,
};

// ─────────────────────────────────────────────────────────────────────────────
// Data structures
// ─────────────────────────────────────────────────────────────────────────────

/// Campaign state (Borsh, 58 bytes).
#[derive(BorshSerialize, BorshDeserialize, Debug, Clone)]
pub struct Campaign {
    pub creator:  Pubkey,   // 32
    pub goal:     u64,      //  8
    pub raised:   u64,      //  8
    pub deadline: i64,      //  8
    pub claimed:  bool,     //  1
    pub bump:     u8,       //  1   vault PDA bump
}
impl Campaign {
    pub const LEN: usize = 32 + 8 + 8 + 8 + 1 + 1; // 58
}

/// Per-donor contribution receipt — stored in a separate PDA per (campaign, donor).
///
/// Borsh layout (40 bytes):
///   donor    Pubkey  32
///   amount   u64      8
#[derive(BorshSerialize, BorshDeserialize, Debug, Clone)]
pub struct Contribution {
    pub donor:  Pubkey, // 32
    pub amount: u64,    //  8
}
impl Contribution {
    pub const LEN: usize = 32 + 8; // 40
}

// ─────────────────────────────────────────────────────────────────────────────
// Errors
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CrowdfundingError {
    DeadlineInPast      = 0,
    CampaignEnded       = 1,
    GoalNotReached      = 2,
    GoalReached         = 3,
    AlreadyClaimed      = 4,
    NotCreator          = 5,
    CampaignActive      = 6,
    InvalidAccount      = 7,
    InsufficientFunds   = 8,
    AccountNotWritable  = 9,
    NoContributionFound = 10,
    RefundExceedsAmount = 11,
}
impl From<CrowdfundingError> for ProgramError {
    fn from(e: CrowdfundingError) -> Self { ProgramError::Custom(e as u32) }
}

// ─────────────────────────────────────────────────────────────────────────────
// Program ID
// ─────────────────────────────────────────────────────────────────────────────

solana_program::declare_id!("DKsRhfniEEv3EcNgvbid11aDAAC3Mbsxui3rTQnU5GS3");

solana_program::entrypoint!(process_instruction);

// ─────────────────────────────────────────────────────────────────────────────
// Dispatch
// ─────────────────────────────────────────────────────────────────────────────

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
        3 => refund(program_id, accounts),
        _ => Err(ProgramError::InvalidInstructionData),
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// PDA helpers
// ─────────────────────────────────────────────────────────────────────────────

fn find_vault_pda(campaign_key: &Pubkey, program_id: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[b"vault", campaign_key.as_ref()], program_id)
}

fn find_contribution_pda(
    campaign_key: &Pubkey,
    donor_key:    &Pubkey,
    program_id:   &Pubkey,
) -> (Pubkey, u8) {
    Pubkey::find_program_address(
        &[b"contribution", campaign_key.as_ref(), donor_key.as_ref()],
        program_id,
    )
}

// ─────────────────────────────────────────────────────────────────────────────
// Validation helpers
// ─────────────────────────────────────────────────────────────────────────────

/// Verify account is writable.
fn require_writable(account: &AccountInfo) -> ProgramResult {
    if !account.is_writable {
        msg!("Error: Account {} must be writable", account.key);
        return Err(CrowdfundingError::AccountNotWritable.into());
    }
    Ok(())
}

/// Verify account owner matches expected program.
fn require_owner(account: &AccountInfo, expected: &Pubkey) -> ProgramResult {
    if account.owner != expected {
        msg!(
            "Error: Account {} owner {} != expected {}",
            account.key, account.owner, expected
        );
        return Err(CrowdfundingError::InvalidAccount.into());
    }
    Ok(())
}

// ═════════════════════════════════════════════════════════════════════════════
// Instruction 0 — CreateCampaign
//
// Accounts:
//   0  creator          signer, writable
//   1  campaign         signer, writable   (new, program-owned)
//   2  vault            writable           (PDA, system-owned)
//   3  system_program
//
// NOTE: rent_sysvar REMOVED — we use Rent::get() instead (non-deprecated).
// NOTE: Vault is initialised via transfer + assign instead of create_account
//       to prevent the pre-funding attack (AccountAlreadyInitialized).
// ═════════════════════════════════════════════════════════════════════════════

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
    let sys_program      = next_account_info(iter)?;

    if !creator.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    require_writable(campaign_account)?;
    require_writable(vault_account)?;

    // Deadline must be in the future
    let clock = Clock::get()?;
    if deadline <= clock.unix_timestamp {
        msg!("Error: Deadline must be in the future");
        return Err(CrowdfundingError::DeadlineInPast.into());
    }

    // Derive + verify vault PDA
    let (vault_pda, bump) = find_vault_pda(campaign_account.key, program_id);
    if vault_pda != *vault_account.key {
        msg!("Error: Vault PDA mismatch");
        return Err(CrowdfundingError::InvalidAccount.into());
    }

    // FIX: Use Rent::get() instead of deprecated Rent::from_account_info
    let rent = Rent::get()?;

    // ── Create campaign account ───────────────────────────────────────────────
    invoke(
        &system_instruction::create_account(
            creator.key,
            campaign_account.key,
            rent.minimum_balance(Campaign::LEN),
            Campaign::LEN as u64,
            program_id,
        ),
        &[creator.clone(), campaign_account.clone(), sys_program.clone()],
    )?;

    // ── Initialise vault PDA (pre-fund-safe) ─────────────────────────────────
    // Instead of create_account (which fails if PDA is already funded by an
    // attacker), we: (1) transfer rent-exempt minimum, (2) allocate 0 bytes,
    // (3) assign to system program.  Steps 2+3 are no-ops on a fresh PDA but
    // succeed even if the PDA already has lamports (no AccountAlreadyInitialized).
    let vault_rent = rent.minimum_balance(0);
    let current    = vault_account.lamports();

    if current < vault_rent {
        let deficit = vault_rent - current;
        invoke_signed(
            &system_instruction::transfer(creator.key, vault_account.key, deficit),
            &[creator.clone(), vault_account.clone(), sys_program.clone()],
            &[&[b"vault", campaign_account.key.as_ref(), &[bump]]],
        )?;
    }
    // allocate(0) + assign(system_program) — idempotent, safe if already init
    invoke_signed(
        &system_instruction::allocate(vault_account.key, 0),
        &[vault_account.clone(), sys_program.clone()],
        &[&[b"vault", campaign_account.key.as_ref(), &[bump]]],
    )?;
    invoke_signed(
        &system_instruction::assign(vault_account.key, &system_program::id()),
        &[vault_account.clone(), sys_program.clone()],
        &[&[b"vault", campaign_account.key.as_ref(), &[bump]]],
    )?;

    // ── Persist campaign state ────────────────────────────────────────────────
    let campaign = Campaign {
        creator: *creator.key, goal, raised: 0, deadline, claimed: false, bump,
    };
    campaign.serialize(&mut &mut campaign_account.data.borrow_mut()[..])?;

    msg!("Campaign created: goal={}, deadline={}", goal, deadline);
    msg!("Vault PDA: {}", vault_pda);
    Ok(())
}

// ═════════════════════════════════════════════════════════════════════════════
// Instruction 1 — Contribute
//
// Accounts:
//   0  donor            signer, writable
//   1  campaign         writable           (owner = this program — validated)
//   2  vault            writable           PDA
//   3  contribution     writable           PDA ["contribution", campaign, donor]
//   4  system_program
//
// Per-donor contribution tracking: a Contribution PDA is created on first
// donation and updated (amount += new_amount) on subsequent ones.
// ═════════════════════════════════════════════════════════════════════════════

fn contribute(
    program_id: &Pubkey,
    accounts:   &[AccountInfo],
    amount:     u64,
) -> ProgramResult {
    let iter = &mut accounts.iter();

    let donor            = next_account_info(iter)?;
    let campaign_account = next_account_info(iter)?;
    let vault_account    = next_account_info(iter)?;
    let contrib_account  = next_account_info(iter)?;
    let sys_program      = next_account_info(iter)?;

    if !donor.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    require_writable(campaign_account)?;
    require_writable(vault_account)?;
    require_writable(contrib_account)?;

    // FIX: Validate campaign account is owned by this program
    require_owner(campaign_account, program_id)?;

    // Verify vault PDA
    let (vault_pda, _) = find_vault_pda(campaign_account.key, program_id);
    if vault_pda != *vault_account.key {
        msg!("Error: Vault PDA mismatch");
        return Err(CrowdfundingError::InvalidAccount.into());
    }

    // Verify contribution PDA
    let (contrib_pda, contrib_bump) =
        find_contribution_pda(campaign_account.key, donor.key, program_id);
    if contrib_pda != *contrib_account.key {
        msg!("Error: Contribution PDA mismatch");
        return Err(CrowdfundingError::InvalidAccount.into());
    }

    let mut campaign = Campaign::try_from_slice(&campaign_account.data.borrow())?;

    let clock = Clock::get()?;
    if clock.unix_timestamp >= campaign.deadline {
        msg!("Error: Campaign has ended");
        return Err(CrowdfundingError::CampaignEnded.into());
    }

    // ── Transfer SOL: donor → vault ──────────────────────────────────────────
    invoke(
        &system_instruction::transfer(donor.key, vault_account.key, amount),
        &[donor.clone(), vault_account.clone(), sys_program.clone()],
    )?;

    // ── Update / create contribution PDA ─────────────────────────────────────
    let contrib_seeds: &[&[u8]] = &[
        b"contribution", campaign_account.key.as_ref(), donor.key.as_ref(), &[contrib_bump],
    ];

    if contrib_account.data_len() == 0 {
        // First-time donor — create the contribution account
        let rent = Rent::get()?;
        invoke_signed(
            &system_instruction::create_account(
                donor.key,
                contrib_account.key,
                rent.minimum_balance(Contribution::LEN),
                Contribution::LEN as u64,
                program_id,
            ),
            &[donor.clone(), contrib_account.clone(), sys_program.clone()],
            &[contrib_seeds],
        )?;
        let contribution = Contribution { donor: *donor.key, amount };
        contribution.serialize(&mut &mut contrib_account.data.borrow_mut()[..])?;
    } else {
        // Returning donor — update existing
        require_owner(contrib_account, program_id)?;
        let mut contribution = Contribution::try_from_slice(&contrib_account.data.borrow())?;
        if contribution.donor != *donor.key {
            msg!("Error: Contribution account donor mismatch");
            return Err(CrowdfundingError::InvalidAccount.into());
        }
        contribution.amount = contribution.amount
            .checked_add(amount)
            .ok_or(ProgramError::ArithmeticOverflow)?;
        contribution.serialize(&mut &mut contrib_account.data.borrow_mut()[..])?;
    }

    // ── Update campaign raised counter ───────────────────────────────────────
    campaign.raised = campaign.raised
        .checked_add(amount)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    campaign.serialize(&mut &mut campaign_account.data.borrow_mut()[..])?;

    msg!("Contributed: {} lamports, total={}", amount, campaign.raised);
    Ok(())
}

// ═════════════════════════════════════════════════════════════════════════════
// Instruction 2 — Withdraw
//
// Accounts:
//   0  creator          signer, writable
//   1  campaign         writable           (owner = this program)
//   2  vault            writable           PDA
//   3  system_program
// ═════════════════════════════════════════════════════════════════════════════

fn withdraw(
    program_id: &Pubkey,
    accounts:   &[AccountInfo],
) -> ProgramResult {
    let iter = &mut accounts.iter();

    let creator          = next_account_info(iter)?;
    let campaign_account = next_account_info(iter)?;
    let vault_account    = next_account_info(iter)?;
    let sys_program      = next_account_info(iter)?;

    if !creator.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    require_writable(campaign_account)?;
    require_writable(vault_account)?;
    require_owner(campaign_account, program_id)?;

    let mut campaign = Campaign::try_from_slice(&campaign_account.data.borrow())?;

    let (vault_pda, _) = find_vault_pda(campaign_account.key, program_id);
    if vault_pda != *vault_account.key {
        msg!("Error: Vault PDA mismatch");
        return Err(CrowdfundingError::InvalidAccount.into());
    }
    let bump = campaign.bump;

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

    let vault_lamports = **vault_account.lamports.borrow();
    if vault_lamports == 0 {
        msg!("Error: Vault is empty");
        return Err(CrowdfundingError::InsufficientFunds.into());
    }

    invoke_signed(
        &system_instruction::transfer(vault_account.key, creator.key, vault_lamports),
        &[vault_account.clone(), creator.clone(), sys_program.clone()],
        &[&[b"vault", campaign_account.key.as_ref(), &[bump]]],
    )?;

    campaign.claimed = true;
    campaign.raised  = 0;
    campaign.serialize(&mut &mut campaign_account.data.borrow_mut()[..])?;

    msg!("Withdrawn: {} lamports to creator", vault_lamports);
    Ok(())
}

// ═════════════════════════════════════════════════════════════════════════════
// Instruction 3 — Refund
//
// FIX: Refund amount is now determined by the on-chain Contribution PDA —
// callers can no longer specify an arbitrary amount.
//
// Accounts:
//   0  donor            signer, writable
//   1  campaign         writable           (owner = this program)
//   2  vault            writable           PDA
//   3  contribution     writable           PDA ["contribution", campaign, donor]
//   4  system_program
// ═════════════════════════════════════════════════════════════════════════════

fn refund(
    program_id: &Pubkey,
    accounts:   &[AccountInfo],
) -> ProgramResult {
    let iter = &mut accounts.iter();

    let donor            = next_account_info(iter)?;
    let campaign_account = next_account_info(iter)?;
    let vault_account    = next_account_info(iter)?;
    let contrib_account  = next_account_info(iter)?;
    let sys_program      = next_account_info(iter)?;

    if !donor.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    require_writable(campaign_account)?;
    require_writable(vault_account)?;
    require_writable(contrib_account)?;
    require_owner(campaign_account, program_id)?;
    require_owner(contrib_account, program_id)?;

    let campaign = Campaign::try_from_slice(&campaign_account.data.borrow())?;

    // Verify vault PDA
    let (vault_pda, _) = find_vault_pda(campaign_account.key, program_id);
    if vault_pda != *vault_account.key {
        msg!("Error: Vault PDA mismatch");
        return Err(CrowdfundingError::InvalidAccount.into());
    }
    let bump = campaign.bump;

    // Verify contribution PDA
    let (contrib_pda, _) =
        find_contribution_pda(campaign_account.key, donor.key, program_id);
    if contrib_pda != *contrib_account.key {
        msg!("Error: Contribution PDA mismatch");
        return Err(CrowdfundingError::InvalidAccount.into());
    }

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

    // Read the donor's actual contribution — prevents arbitrary drain
    let contribution = Contribution::try_from_slice(&contrib_account.data.borrow())?;
    if contribution.donor != *donor.key {
        msg!("Error: Contribution donor mismatch");
        return Err(CrowdfundingError::InvalidAccount.into());
    }
    let refund_amount = contribution.amount;
    if refund_amount == 0 {
        msg!("Error: No contribution to refund");
        return Err(CrowdfundingError::NoContributionFound.into());
    }

    let vault_lamports = **vault_account.lamports.borrow();
    if vault_lamports < refund_amount {
        msg!("Error: Insufficient funds in vault");
        return Err(CrowdfundingError::InsufficientFunds.into());
    }

    // Transfer refund from vault → donor
    invoke_signed(
        &system_instruction::transfer(vault_account.key, donor.key, refund_amount),
        &[vault_account.clone(), donor.clone(), sys_program.clone()],
        &[&[b"vault", campaign_account.key.as_ref(), &[bump]]],
    )?;

    // Zero out the contribution to prevent double-refund.
    // Close the account: transfer rent back to donor, zero data.
    let contrib_lamports = **contrib_account.lamports.borrow();
    **contrib_account.lamports.borrow_mut() = 0;
    **donor.lamports.borrow_mut() = donor.lamports()
        .checked_add(contrib_lamports)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    // Zero the data so it can't be deserialized again
    let mut data = contrib_account.data.borrow_mut();
    for byte in data.iter_mut() {
        *byte = 0;
    }

    msg!("Refunded: {} lamports to {}", refund_amount, donor.key);
    Ok(())
}
