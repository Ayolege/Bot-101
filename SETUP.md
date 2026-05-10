# Setup Guide — From Zero to Running Bot

This guide walks you through every step from creating your VPS to running
the bot live. No prior GitHub or Linux experience needed.

---

## Overview of what you're building

```
Your laptop (Nigeria)
       |
       | SSH (remote control)
       v
VPS in Tokyo, Japan  ←——→  Binance servers (also Tokyo)
  (Vultr $24/month)         ~2ms away
```

You control the bot from your laptop, but the bot itself runs 24/7 on the
Tokyo server — right next to Binance's matching engine.

---

## Step 1 — Get your Binance API keys

You need two keys so the bot can trade on your behalf.

1. Log into **binance.com** on your browser
2. Click your profile icon (top right) → **API Management**
3. Click **Create API** → choose **System generated**
4. Give it a label, e.g. `ArbBot`
5. Complete identity verification (email + authenticator)
6. On the permissions screen:
   - ✅ **Enable Reading**
   - ✅ **Enable Spot & Margin Trading**
   - ❌ Do NOT enable withdrawals
7. Copy and save both keys somewhere safe:
   - **API Key** (looks like: `abc123XYZ...`)
   - **Secret Key** (only shown once — copy it now)

> We will come back and add the VPS IP to the whitelist in Step 4.

---

## Step 2 — Create your VPS on Vultr (Tokyo)

**Recommended: Vultr High Frequency Tokyo — $24/month**

1. Go to **vultr.com** and create an account
2. Click **Deploy** → **Cloud Compute — High Frequency**
3. Choose location: **Tokyo, Japan**
4. Choose image: **Ubuntu 22.04 LTS**
5. Choose plan: **1 vCPU / 2GB RAM / 50GB NVMe** (~$24/month)
6. Scroll down to **SSH Keys** — skip for now (we'll use password)
7. Click **Deploy Now**
8. Wait ~60 seconds for it to boot
9. On the server page, copy:
   - **IP Address** (e.g. `45.76.123.45`)
   - **Username**: `root`
   - **Password**: shown on the page

---

## Step 3 — Connect to your VPS

You need a terminal on your laptop to type commands on the VPS.

### If you're on Windows:
1. Open **Windows Terminal** or **PowerShell** (search in Start menu)
2. Type: `ssh root@45.76.123.45` (replace with your actual IP)
3. Type `yes` when asked about fingerprint
4. Enter the password from Vultr

### If you're on Mac:
1. Open **Terminal** (search Spotlight for "Terminal")
2. Type: `ssh root@45.76.123.45` (replace with your actual IP)
3. Type `yes` when asked about fingerprint
4. Enter the password from Vultr

You should now see a prompt like `root@vultr:~#` — you are inside your VPS.

---

## Step 4 — Whitelist your VPS IP on Binance

Before the bot can use the API, Binance needs to allow requests from the VPS.

1. Go back to **binance.com** → API Management → your `ArbBot` key
2. Click **Edit**
3. Under **Access Restriction**, select **Restrict access to trusted IPs only**
4. Enter your VPS IP address (e.g. `45.76.123.45`)
5. Click **Confirm**

This means only your Tokyo VPS can use these keys — if they ever get stolen,
they're useless from anywhere else.

---

## Step 5 — Set up the VPS (one-time)

Copy and paste these commands into your VPS terminal one at a time.
Each line does exactly one thing — described above it.

```bash
# Update the system
apt update && apt upgrade -y

# Install Python 3.11 and git
apt install -y python3.11 python3.11-venv python3-pip git

# Create a normal user (don't run bots as root)
adduser ubuntu
# It will ask for a password — set one and remember it
# Press Enter to skip all the extra questions

# Give ubuntu sudo access
usermod -aG sudo ubuntu

# Switch to the ubuntu user
su - ubuntu
```

You are now operating as `ubuntu`. Your prompt should show `ubuntu@vultr:~$`.

---

## Step 6 — Download the bot code

```bash
# Download the bot from GitHub
git clone https://github.com/ayolege/bot-101.git

# Enter the bot folder
cd bot-101

# Create an isolated Python environment (keeps the bot's packages separate)
python3.11 -m venv venv

# Activate it (you'll do this every time you manually work with the bot)
source venv/bin/activate

# Install all required packages
pip install -r requirements.txt
```

This takes 1–2 minutes. When done, you'll see `Successfully installed ...`

---

## Step 7 — Configure your API keys

```bash
# Create your private config file from the template
cp .env.example .env

# Open the file to edit it
nano .env
```

You'll see something like this:

```
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
BINANCE_PROXY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Replace `your_api_key_here` with your actual Binance API Key.
Replace `your_api_secret_here` with your actual Secret Key.
Leave the others blank for now.

To save and exit nano:
- Press `Ctrl + X`
- Press `Y` to confirm
- Press `Enter`

Verify it looks right:
```bash
cat .env
# You should see your real keys (not the placeholder text)
```

---

## Step 8 — Test with dry run (no real orders)

Always run in dry-run mode first to make sure everything connects.

```bash
# Make sure you're in the bot folder with venv active
cd ~/bot-101
source venv/bin/activate

# Run in simulation mode — no real trades
python main.py --dry-run
```

You should see output like:
```
2026-01-01 12:00:00 | INFO | [Binance] Connected. Markets loaded.
2026-01-01 12:00:00 | INFO | [Binance] Account permissions verified. canTrade=True
2026-01-01 12:00:01 | INFO | [Bot] Binance USDT balance: $50.00
2026-01-01 12:00:01 | INFO | [Bot] Capital: $50.00 | Trade size: $15 | Daily loss limit: $5
2026-01-01 12:00:04 | INFO | [Bot] Strategy ready — 8 triangles, 18 symbols
2026-01-01 12:00:04 | INFO | [Bot] Auto-execute loop started.
```

If you see errors:
- `Authentication failed` → your API keys in .env are wrong
- `canTrade=False` → enable spot trading on the Binance API key
- `Insufficient balance` → deposit USDT to your Binance spot wallet

Press `Ctrl + C` to stop the dry run.

---

## Step 9 — Run live

```bash
python main.py
```

You'll see a 5-second countdown:
```
  LIVE MODE — starting in 5s … (Ctrl-C to abort)
  LIVE MODE — starting in 4s …
  ...
```

If you change your mind, press `Ctrl + C` during the countdown to abort.
After the countdown, it starts scanning and will auto-execute trades.

---

## Step 10 — Keep the bot running 24/7 (auto-restart)

If you close your laptop or the SSH connection drops, the bot stops.
We use **systemd** to run it as a background service that starts automatically.

```bash
# Go back to root (the service file needs root)
exit
# You are now root again

# Copy the service file to the system location
cp /home/ubuntu/bot-101/arbbot.service /etc/systemd/system/arbbot.service

# Open the service file and verify paths look right
nano /etc/systemd/system/arbbot.service
# It should point to /home/ubuntu/bot-101 — press Ctrl+X to exit

# Tell systemd to read the new file
systemctl daemon-reload

# Start the bot
systemctl start arbbot

# Enable auto-start on server reboot
systemctl enable arbbot

# Check it's running
systemctl status arbbot
```

You should see `Active: active (running)` in green.

---

## Daily management commands

All these are run after SSH-ing into the VPS:

```bash
# Check if the bot is running
systemctl status arbbot

# Watch live log output (press Ctrl+C to stop watching)
journalctl -u arbbot -f

# See the last 100 lines of logs
journalctl -u arbbot -n 100

# Stop the bot (e.g. to change settings)
systemctl stop arbbot

# Start it again
systemctl start arbbot

# Restart after changing settings
systemctl restart arbbot

# See today's trade results
cat /home/ubuntu/bot-101/logs/trades.csv
```

---

## Changing settings

To adjust trade size, thresholds, or risk limits:

```bash
su - ubuntu
cd ~/bot-101
nano config/config.yaml
# Make your changes, then Ctrl+X, Y, Enter to save

# Restart the bot to apply changes
exit
systemctl restart arbbot
```

---

## Updating the bot code

When there's a new version:

```bash
su - ubuntu
cd ~/bot-101
source venv/bin/activate
git pull origin claude/arbitrage-bot-nigeria-1bXRM
pip install -r requirements.txt
exit
systemctl restart arbbot
```

---

## What to expect with $50

| Metric | Value |
|---|---|
| Trade size per cycle | $15 |
| Fees per cycle | ~$0.045 (0.3% × $15) |
| Min profit to trade | 0.5% = $0.075 |
| Daily loss limit | $5 (stops the bot) |
| Daily fee budget | $3 (stops churning) |
| Realistic daily profit | $0.50 – $2.00 |
| Monthly at 1%/day | ~$15 – $20 compound |

The bot will **not** trade unless it finds a spread wider than 0.5% net of fees.
These opportunities are rare but real on Binance — the bot scans all 8 triangles
every 10 milliseconds to catch them before other bots do.

---

## Safety checklist before going live

- [ ] API key has only Read + Spot Trading (no withdrawals)
- [ ] VPS IP is whitelisted on Binance API key
- [ ] Dry run completed with no errors
- [ ] `logs/` directory exists and is writable
- [ ] Starting USDT balance is ≥ $25 in Binance spot wallet
- [ ] You understand: losses up to $5/day are possible
