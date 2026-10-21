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

Security

Store credentials in a local configuration file or environment variables. Never commit tokens, passwords, or private credentials to GitHub.

Project Goal

Reliable stock detection for the exact ₹1,000 India PSN product at each configured store, with a readable Discord report and minimal setup.
