#!/usr/bin/env python3
"""
Generate StringSession for Telethon.
This script creates a StringSession that can be used in cloud deployments.

Usage:
1. Update API_ID and API_HASH below (or set as environment variables)
2. Run: python3 make_session.py
3. Copy the SESSION_STRING output and use it as TG_SESSION_STRING in your cloud deployment
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession
import os

# You can either set these here or use environment variables
API_ID = int(os.getenv("TG_API_ID", "25989420"))  # Replace with your API ID
API_HASH = os.getenv("TG_API_HASH", "13144bcea846e6c8b3fe92b185ffde1f")   # Replace with your API Hash

# Basic validation (check if using default placeholder values)
if API_ID == 123 or API_HASH == "xxx" or not API_HASH:
    print("⚠️  Please update API_ID and API_HASH in this file or set TG_API_ID and TG_API_HASH environment variables")
    exit(1)

print("📱 Starting Telegram client...")

with TelegramClient("tele_alert", API_ID, API_HASH) as client:
    if not client.is_user_authorized():
        print("❌ Not authorized. Please run forwarder.py first to create a session file.")
        print("   The script will ask for your phone number and code to authorize.")
        exit(1)
    
    print("✅ Using existing session file")
    
    session_string = StringSession.save(client.session)
    print("\n" + "="*60)
    print("✅ SESSION_STRING generated successfully!")
    print("="*60)
    print(f"\nSESSION_STRING = {session_string}\n")
    print("Copy this value and set it as TG_SESSION_STRING in your cloud deployment.")
    print("="*60)
