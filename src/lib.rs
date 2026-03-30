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
    pub creator: Pubkey,      // 32 bytes
    pub goal: u64,            //  8 bytes
    pub raised: u64,          //  8 bytes
    pub deadline: i64,        //  8 bytes
    pub claimed: bool,        //  1 byte
    pub bump: u8,             //  1 byte
    // Total Borsh size: 58 bytes
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
solana_program::declare_id!("3Dc6ZJsWiQm6CmDUt5MY4izbdLgpBU2KbhfSmqpVcayM");

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
fn create_campaign(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    goal: u64,
    deadline: i64,
) -> ProgramResult {
    let accounts_iter = &mut accounts.iter();

    let creator         = next_account_info(accounts_iter)?;
    let campaign_account = next_account_info(accounts_iter)?;
    let system_program  = next_account_info(accounts_iter)?;
    let rent            = next_account_info(accounts_iter)?;

    if !creator.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }

    let clock = Clock::get()?;
    if deadline <= clock.unix_timestamp {
        msg!("Error: Deadline must be in the future");
        return Err(CrowdfundingError::DeadlineInPast.into());
    }

    let (vault_pda, bump) = Pubkey::find_program_address(
        &[b"vault", campaign_account.key.as_ref()],
        program_id,
    );

    // FIX: correct Borsh size for Campaign struct:
    //   creator(32) + goal(8) + raised(8) + deadline(8) + claimed(1) + bump(1) = 58
    // The old code used 8+32+8+8+8+1+1 = 66, treating the leading 8 as an
    // Anchor-style discriminator — but this is a native program. Borsh writes
    // exactly 58 bytes. Allocating 66 left 8 garbage bytes at the end, causing
    // every subsequent try_from_slice to fail with BorshIoError.
    let campaign_data_len = 32 + 8 + 8 + 8 + 1 + 1; // = 58

    let rent_data = Rent::from_account_info(rent)?;
    let rent_exempt_minimum = rent_data.minimum_balance(campaign_data_len);

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
fn contribute(
    _program_id: &Pubkey,
    accounts: &[AccountInfo],
    amount: u64,
) -> ProgramResult {
    let accounts_iter = &mut accounts.iter();

    let donor            = next_account_info(accounts_iter)?;
    let campaign_account = next_account_info(accounts_iter)?;
    let system_program   = next_account_info(accounts_iter)?;

    if !donor.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }

    let mut campaign = Campaign::try_from_slice(&campaign_account.data.borrow())?;

    let clock = Clock::get()?;
    if clock.unix_timestamp >= campaign.deadline {
        msg!("Error: Campaign has ended");
        return Err(CrowdfundingError::CampaignEnded.into());
    }

    // Campaign account itself holds the funds (it is the vault)
    invoke(
        &system_instruction::transfer(donor.key, campaign_account.key, amount),
        &[
            donor.clone(),
            campaign_account.clone(),
            system_program.clone(),
        ],
    )?;

    campaign.raised = campaign
        .raised
        .checked_add(amount)
        .ok_or(ProgramError::ArithmeticOverflow)?;

    campaign.serialize(&mut &mut campaign_account.data.borrow_mut()[..])?;

    msg!("Contributed: {} lamports, total={}", amount, campaign.raised);

    Ok(())
}

/// Withdraw funds (creator only, after deadline, if goal reached)
fn withdraw(
    _program_id: &Pubkey,
    accounts: &[AccountInfo],
) -> ProgramResult {
    let accounts_iter = &mut accounts.iter();

    let creator          = next_account_info(accounts_iter)?;
    let campaign_account = next_account_info(accounts_iter)?;

    if !creator.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }

    let mut campaign = Campaign::try_from_slice(&campaign_account.data.borrow())?;

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
        msg!("Error: Campaign goal not reached");
        return Err(CrowdfundingError::GoalNotReached.into());
    }

    if campaign.claimed {
        msg!("Error: Already claimed");
        return Err(CrowdfundingError::AlreadyClaimed.into());
    }

    let amount           = campaign.raised;
    let campaign_lamports = **campaign_account.lamports.borrow();
    let creator_lamports  = **creator.lamports.borrow();

    **campaign_account.lamports.borrow_mut() = 0;
    **creator.lamports.borrow_mut() = creator_lamports
        .checked_add(campaign_lamports)
        .ok_or(ProgramError::ArithmeticOverflow)?;

    campaign.claimed = true;
    campaign.raised  = 0;

    campaign.serialize(&mut &mut campaign_account.data.borrow_mut()[..])?;

    msg!("Withdrawn: {} lamports", amount);

    Ok(())
}

/// Refund (donors only, after deadline, if goal NOT reached)
fn refund(
    _program_id: &Pubkey,
    accounts: &[AccountInfo],
    amount: u64,
) -> ProgramResult {
    let accounts_iter = &mut accounts.iter();

    let donor            = next_account_info(accounts_iter)?;
    let campaign_account = next_account_info(accounts_iter)?;

    let campaign = Campaign::try_from_slice(&campaign_account.data.borrow())?;

    let clock = Clock::get()?;
    if clock.unix_timestamp < campaign.deadline {
        msg!("Error: Campaign still active");
        return Err(CrowdfundingError::CampaignActive.into());
    }

    if campaign.raised >= campaign.goal {
        msg!("Error: Campaign goal reached - cannot refund");
        return Err(CrowdfundingError::GoalReached.into());
    }

    let campaign_lamports = **campaign_account.lamports.borrow();
    if campaign_lamports < amount {
        msg!("Error: Insufficient funds in campaign");
        return Err(CrowdfundingError::InsufficientFunds.into());
    }

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
