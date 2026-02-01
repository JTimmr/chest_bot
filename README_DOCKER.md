# Docker Setup for Chest Bot

## Prerequisites
- Docker installed on your system
- Docker Compose (usually comes with Docker Desktop)

## Setup Instructions

1. **Create a `.env` file** in the `chest_bot` directory with your credentials:
   ```
   DISCORD_BOT_TOKEN=your_discord_bot_token_here
   OPENAI_API_KEY=your_openai_api_key_here
   HELIUS_API_KEY=your_helius_api_key_here
   FARTBOY_MINT=your_fartboy_mint_here
   WAR_CHEST_WALLET=your_war_chest_wallet_here
   ```
   Adjust or add variables to match your bot's needs.

2. **Build and run with Docker Compose** (recommended):
   ```bash
   docker-compose up -d
   ```
   
   To view logs:
   ```bash
   docker-compose logs -f
   ```
   
   To stop:
   ```bash
   docker-compose down
   ```

3. **Or build and run with Docker directly**:
   ```bash
   # Build the image
   docker build -t chest-bot .
   
   # Run the container
   docker run -d --name chest-discord-bot --env-file .env --restart unless-stopped chest-bot
   
   # View logs
   docker logs -f chest-discord-bot
   
   # Stop the container
   docker stop chest-discord-bot
   docker rm chest-discord-bot
   ```

## Notes
- The container will automatically restart unless stopped manually
- Make sure your `.env` file is in the same directory as `docker-compose.yml`
- The bot will load environment variables from the `.env` file at startup
