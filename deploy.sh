#!/bin/bash
# Solana Crowdfunding Platform - Deployment Script

export PATH="$HOME/.cargo/bin:$PATH"

echo "============================================"
echo "Solana Crowdfunding Platform - Deployment"
echo "============================================"
echo ""

# Check if program is built
PROGRAM_SO="target/sbpf-solana-solana/release/solana_crowdfunding.so"
if [ ! -f "$PROGRAM_SO" ]; then
    echo "Building program..."
    cargo build-sbf
fi

echo "Program location: $PROGRAM_SO"
echo "Program ID: CrwdN8ruKmWC3uxRWD9LU1RqoT4V3WQ3iRCJ5rRDxS3q"
echo ""

# Check balance
echo "Checking wallet balance..."
solana balance

echo ""
echo "Deploying to devnet..."
solana program deploy $PROGRAM_SO

echo ""
echo "============================================"
echo "Deployment Complete!"
echo "============================================"
echo "Program ID: CrwdN8ruKmWC3uxRWD9LU1RqoT4V3WQ3iRCJ5rRDxS3q"
echo ""
echo "Next steps:"
echo "1. Request airdrop: solana airdrop 5"
echo "2. Run tests using the client"
echo "3. Interact with the program"