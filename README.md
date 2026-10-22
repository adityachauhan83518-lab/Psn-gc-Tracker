PSN Stock Tracker

A Python Discord bot that monitors eight Indian stores for the ₹1,000 India PlayStation Network Wallet Top-Up.

Features

- Checks all eight stores every 30 seconds.
- Displays a clean, human-readable Discord status report.
- Detects Out of Stock and Sold Out indicators.
- Reports uncertain or failed checks without falsely claiming stock availability.
- Sends alerts when stock is detected as available.
- Prevents repeated alerts while the same stock remains available.
- Keeps diagnostic logs for troubleshooting.

Requirements

- Python 3.10 or newer
- Discord bot token
- Discord channel ID
- Numeric admin user ID

Setup

1. Install dependencies with `python -m pip install -r requirements.txt`.
2. Create a Discord application and bot. In the Developer Portal, enable **Message Content Intent** under Bot > Privileged Gateway Intents. Invite it to your server with the `bot` and `applications.commands` scopes and `View Channel` and `Send Messages` permissions in the channels where it will be used.
3. Set these environment variables in your local terminal:

	```sh
	export DISCORD_BOT_TOKEN="your-bot-token"
	export DISCORD_CHANNEL_ID="your-alert-channel-id"
	export DISCORD_ADMIN_USER_ID="your-numeric-user-id"
	```

4. Start the tracker with `python tracker.py`.

The bot checks the stores every 30 seconds and posts an alert when a store changes to an in-stock result. Use `!ping` to confirm the bot is alive, see whether polling is running, and see when its last store scan completed. Use `/status` for an on-demand report; only the configured admin user can run it. Logs are written to the process console.

Security

Keep credentials in local environment variables. Never commit tokens, passwords, or private credentials to GitHub, and do not send the bot token in chat.

Project Goal

Reliable stock detection for the exact ₹1,000 India PSN product at each configured store, with a readable Discord report and minimal setup.
