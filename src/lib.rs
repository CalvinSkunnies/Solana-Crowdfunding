use borsh::{BorshDeserialize, BorshSerialize};
use solana_program::{
    account_info::{next_account_info, AccountInfo},
    clock::Clock,
    entrypoint::ProgramResult,
    msg,
    program::invoke,
    program_error::ProgramError,
    pubkey::Pubkey,
    rent::Rent,
    system_instruction,
    sysvar::Sysvar,
};

/// Data structure for a campaign
#[derive(BorshSerialize, BorshDeserialize, Debug, Clone)]
pub struct Campaign {
    pub creator: Pubkey,      // Who created this
    pub goal: u64,            // Target amount in lamports
    pub raised: u64,          // Current amount raised
    pub deadline: i64,        // When campaign ends (Unix timestamp)
    pub claimed: bool,        // Already withdrawn?
    pub bump: u8,             // PDA bump for vault
}

/// Error codes
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CrowdfundingError {
    DeadlineInPast = 0,
    CampaignEnded = 1,
    GoalNotReached = 2,
    GoalReached = 3,
    AlreadyClaimed = 4,
    NotCreator = 5,
    CampaignActive = 6,
    InvalidAccount = 7,
    InsufficientFunds = 8,
}

impl From<CrowdfundingError> for ProgramError {
    fn from(e: CrowdfundingError) -> Self {
        ProgramError::Custom(e as u32)
    }
}

/// Program ID
solana_program::declare_id!("CrwdN8ruKmWC3uxRWD9LU1RqoT4V3WQ3iRCJ5rRDxS3q");

/// Main processing function
pub fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    instruction_data: &[u8],
) -> ProgramResult {
    if instruction_data.is_empty() {
        return Err(ProgramError::InvalidInstructionData);
    }

    let instruction = instruction_data[0];

    match instruction {
        0 => {
            // CreateCampaign: goal (u64), deadline (i64)
            if instruction_data.len() < 17 {
                return Err(ProgramError::InvalidInstructionData);
            }
            let mut goal_bytes = [0u8; 8];
            let mut deadline_bytes = [0u8; 8];
            goal_bytes.copy_from_slice(&instruction_data[1..9]);
            deadline_bytes.copy_from_slice(&instruction_data[9..17]);
            let goal = u64::from_le_bytes(goal_bytes);
            let deadline = i64::from_le_bytes(deadline_bytes);
            create_campaign(program_id, accounts, goal, deadline)
        }
        1 => {
            // Contribute: amount (u64)
            if instruction_data.len() < 9 {
                return Err(ProgramError::InvalidInstructionData);
            }
            let mut amount_bytes = [0u8; 8];
            amount_bytes.copy_from_slice(&instruction_data[1..9]);
            let amount = u64::from_le_bytes(amount_bytes);
            contribute(program_id, accounts, amount)
        }
        2 => {
            // Withdraw
            withdraw(program_id, accounts)
        }
        3 => {
            // Refund: amount (u64)
            if instruction_data.len() < 9 {
                return Err(ProgramError::InvalidInstructionData);
            }
            let mut amount_bytes = [0u8; 8];
            amount_bytes.copy_from_slice(&instruction_data[1..9]);
            let amount = u64::from_le_bytes(amount_bytes);
            refund(program_id, accounts, amount)
        }
        _ => Err(ProgramError::InvalidInstructionData),
    }
}

/// Create a new campaign
#[allow(clippy::unnecessary_warnings)]
fn create_campaign(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    goal: u64,
    deadline: i64,
) -> ProgramResult {
    let accounts_iter = &mut accounts.iter();

    // Creator account (signer)
    let creator = next_account_info(accounts_iter)?;
    if !creator.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }

    // Campaign account (to be created)
    let campaign_account = next_account_info(accounts_iter)?;

    // System program
    let system_program = next_account_info(accounts_iter)?;

    // Rent sysvar
    let rent = next_account_info(accounts_iter)?;

    // Validate deadline is in the future
    let clock = Clock::get()?;
    if deadline <= clock.unix_timestamp {
        msg!("Error: Deadline must be in the future");
        return Err(CrowdfundingError::DeadlineInPast.into());
    }

    // Derive vault PDA
    let (vault_pda, bump) = Pubkey::find_program_address(
        &[b"vault", campaign_account.key.as_ref()],
        program_id,
    );

    // Calculate rent for campaign account
    let campaign_data_len = 8 + 32 + 8 + 8 + 8 + 1 + 1; // fields
    let rent_data = Rent::from_account_info(rent)?;
    let rent_exempt_minimum = rent_data.minimum_balance(campaign_data_len);

    // Create campaign account
    invoke(
        &system_instruction::create_account(
            creator.key,
            campaign_account.key,
            rent_exempt_minimum,
            campaign_data_len as u64,
            program_id,
        ),
        &[
            creator.clone(),
            campaign_account.clone(),
            system_program.clone(),
        ],
    )?;

    // Initialize campaign data
    let campaign = Campaign {
        creator: *creator.key,
        goal,
        raised: 0,
        deadline,
        claimed: false,
        bump,
    };

    campaign.serialize(&mut &mut campaign_account.data.borrow_mut()[..])?;

    msg!("Campaign created: goal={}, deadline={}", goal, deadline);
    msg!("Vault PDA: {}", vault_pda);

    Ok(())
}

/// Contribute to a campaign
#[allow(clippy::unnecessary_warnings)]
fn contribute(
    _program_id: &Pubkey,
    accounts: &[AccountInfo],
    amount: u64,
) -> ProgramResult {
    let accounts_iter = &mut accounts.iter();

    // Donor account (signer)
    let donor = next_account_info(accounts_iter)?;
    if !donor.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }

    // Campaign account
    let campaign_account = next_account_info(accounts_iter)?;

    // System program
    let system_program = next_account_info(accounts_iter)?;

    // Get campaign data
    let mut campaign = Campaign::try_from_slice(&campaign_account.data.borrow())?;

    // Check campaign hasn't ended
    let clock = Clock::get()?;
    if clock.unix_timestamp >= campaign.deadline {
        msg!("Error: Campaign has ended");
        return Err(CrowdfundingError::CampaignEnded.into());
    }

    // Transfer SOL from donor to campaign (using campaign as vault)
    invoke(
        &system_instruction::transfer(donor.key, campaign_account.key, amount),
        &[
            donor.clone(),
            campaign_account.clone(),
            system_program.clone(),
        ],
    )?;

    // Update raised amount
    campaign.raised = campaign
        .raised
        .checked_add(amount)
        .ok_or(ProgramError::ArithmeticOverflow)?;

    // Serialize back
    campaign.serialize(&mut &mut campaign_account.data.borrow_mut()[..])?;

    msg!(
        "Contributed: {} lamports, total={}",
        amount,
        campaign.raised
    );

    Ok(())
}

/// Withdraw funds (creator only, after deadline, if goal reached)
#[allow(clippy::unnecessary_warnings)]
fn withdraw(
    _program_id: &Pubkey,
    accounts: &[AccountInfo],
) -> ProgramResult {
    let accounts_iter = &mut accounts.iter();

    // Creator account (signer)
    let creator = next_account_info(accounts_iter)?;
    if !creator.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }

    // Campaign account (also acts as vault)
    let campaign_account = next_account_info(accounts_iter)?;

    // Get campaign data
    let mut campaign = Campaign::try_from_slice(&campaign_account.data.borrow())?;

    // Verify caller is creator
    if campaign.creator != *creator.key {
        msg!("Error: Not the campaign creator");
        return Err(CrowdfundingError::NotCreator.into());
    }

    // Check deadline has passed
    let clock = Clock::get()?;
    if clock.unix_timestamp < campaign.deadline {
        msg!("Error: Campaign still active");
        return Err(CrowdfundingError::CampaignActive.into());
    }

    // Check goal reached
    if campaign.raised < campaign.goal {
        msg!("Error: Campaign goal not reached");
        return Err(CrowdfundingError::GoalNotReached.into());
    }

    // Check not already claimed
    if campaign.claimed {
        msg!("Error: Already claimed");
        return Err(CrowdfundingError::AlreadyClaimed.into());
    }

    // Calculate amount to withdraw
    let amount = campaign.raised;

    // Transfer all funds from campaign (vault) to creator
    let campaign_lamports = **campaign_account.lamports.borrow();
    let creator_lamports = **creator.lamports.borrow();

    // Transfer lamports
    **campaign_account.lamports.borrow_mut() = 0;
    **creator.lamports.borrow_mut() = creator_lamports
        .checked_add(campaign_lamports)
        .ok_or(ProgramError::ArithmeticOverflow)?;

    // Mark as claimed
    campaign.claimed = true;
    campaign.raised = 0;

    campaign.serialize(&mut &mut campaign_account.data.borrow_mut()[..])?;

    msg!("Withdrawn: {} lamports", amount);

    Ok(())
}

/// Refund (donors only, after deadline, if goal NOT reached)
#[allow(clippy::unnecessary_warnings)]
fn refund(
    _program_id: &Pubkey,
    accounts: &[AccountInfo],
    amount: u64,
) -> ProgramResult {
    let accounts_iter = &mut accounts.iter();

    // Donor account (signer)
    let donor = next_account_info(accounts_iter)?;
    if !donor.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }

    // Campaign account
    let campaign_account = next_account_info(accounts_iter)?;

    // Get campaign data
    let campaign = Campaign::try_from_slice(&campaign_account.data.borrow())?;

    // Check deadline has passed
    let clock = Clock::get()?;
    if clock.unix_timestamp < campaign.deadline {
        msg!("Error: Campaign still active");
        return Err(CrowdfundingError::CampaignActive.into());
    }

    // Check goal NOT reached
    if campaign.raised >= campaign.goal {
        msg!("Error: Campaign goal reached - cannot refund");
        return Err(CrowdfundingError::GoalReached.into());
    }

    // Check campaign has funds to refund
    let campaign_lamports = **campaign_account.lamports.borrow();
    if campaign_lamports < amount {
        msg!("Error: Insufficient funds in campaign");
        return Err(CrowdfundingError::InsufficientFunds.into());
    }

    // Transfer funds from campaign to donor
    **campaign_account.lamports.borrow_mut() = campaign_lamports
        .checked_sub(amount)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    **donor.lamports.borrow_mut() = donor
        .lamports()
        .checked_add(amount)
        .ok_or(ProgramError::ArithmeticOverflow)?;

    msg!("Refunded: {} lamports", amount);

    Ok(())
}

solana_program::entrypoint!(process_instruction);