#!/usr/bin/env python3
"""
Telegram Message Forwarder with Kyiv Air-Raid Alert Integration

A production-ready script that forwards messages from multiple public Telegram channels
to a private collector channel ONLY during active air-raid alerts in Kyiv.
Uses siren.pp.ua API to monitor alert status in real time.

Setup Instructions:
1. Create and activate virtual environment:
   python3 -m venv venv
   source venv/bin/activate  # On macOS/Linux

2. Install dependencies:
   pip install telethon aiohttp

3. Run the script:
   python -u forwarder.py

4. To re-authorize (if needed):
   rm tele_alert.session
   python -u forwarder.py

5. Auto-start on macOS using launchd:
   Create ~/Library/LaunchAgents/com.telegram.forwarder.plist:
   
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
       <key>Label</key>
       <string>com.telegram.forwarder</string>
       <key>ProgramArguments</key>
       <array>
           <string>/path/to/your/venv/bin/python</string>
           <string>-u</string>
           <string>/path/to/your/forwarder.py</string>
       </array>
       <key>WorkingDirectory</key>
       <string>/path/to/your/script/directory</string>
       <key>RunAtLoad</key>
       <true/>
       <key>KeepAlive</key>
       <true/>
       <key>StandardOutPath</key>
       <string>/path/to/your/logs/forwarder.log</string>
       <key>StandardErrorPath</key>
       <string>/path/to/your/logs/forwarder.error.log</string>
   </dict>
   </plist>
   
   Then load it: launchctl load ~/Library/LaunchAgents/com.telegram.forwarder.plist
"""

import asyncio
import logging
import sys
import json
from datetime import datetime
from typing import List, Union, Optional, Dict, Any
from collections import defaultdict

import aiohttp
from telethon import TelegramClient, events
from telethon.errors import (
    BadMessageError,
    FloodWaitError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    ApiIdInvalidError,
    RPCError,
    AuthKeyError,
    UnauthorizedError,
    UserAlreadyParticipantError
)
from telethon.tl.types import (
    Message,
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaWebPage,
    InputChannel,
    Channel
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.sessions import StringSession
import os

# Configuration - read from environment variables
API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION = os.getenv("TG_SESSION", "tele_alert")
SESSION_STRING = os.getenv("TG_SESSION_STRING", "")  # Optional: for StringSession
SOURCE_CHANNELS = [x.strip() for x in os.getenv(
    "TG_SOURCE_CHANNELS",
    "war_monitor,kievreal1,StrategicaviationT,kiev_levyy_bereg,dangerousKiev,kyiv_nebo"
).split(",") if x.strip()]
TARGET_CHANNEL = int(os.getenv("TG_TARGET_CHANNEL", "0"))

# Alert API configuration
ALERT_API_URL = os.getenv("ALERT_API_URL", "https://siren.pp.ua/api/v3/alerts/31")  # Kyiv region_id = 31
ALERT_CHECK_INTERVAL = 10  # seconds
ALERT_TIMEOUT = 5  # HTTP request timeout in seconds

# Test mode: if enabled, forwards all messages regardless of alert status
TEST_MODE = os.getenv("TEST_MODE", "").lower() in ("true", "1", "yes", "on")

# Media group configuration
MEDIA_GROUP_TIMEOUT = 2  # seconds to wait for all media group messages

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class AlertMonitor:
    """
    Monitors air-raid alert status for Kyiv using siren.pp.ua API.
    Controls an asyncio.Event that gates message forwarding.
    """
    
    def __init__(self, alert_event: asyncio.Event):
        """
        Initialize the alert monitor.
        
        Args:
            alert_event: Event to control message forwarding
        """
        self.alert_event = alert_event
        self.session: Optional[aiohttp.ClientSession] = None
        self.current_alert_status = False
        self.consecutive_errors = 0
        self.max_consecutive_errors = 5
        
    async def start(self) -> None:
        """Start the alert monitoring loop."""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=ALERT_TIMEOUT),
            connector=aiohttp.TCPConnector(limit=10)
        )
        
        logger.info("🔄 Starting alert monitor...")
        
        while True:
            try:
                await self._check_alert_status()
                await asyncio.sleep(ALERT_CHECK_INTERVAL)
                
            except asyncio.CancelledError:
                logger.info("🛑 Alert monitor stopped")
                break
            except Exception as e:
                logger.error(f"❌ Unexpected error in alert monitor: {e}")
                await asyncio.sleep(ALERT_CHECK_INTERVAL)
    
    async def _check_alert_status(self) -> None:
        """Check current alert status and update the event accordingly."""
        try:
            async with self.session.get(ALERT_API_URL) as response:
                if response.status == 200:
                    data = await response.json()
                    await self._process_alert_data(data)
                    self.consecutive_errors = 0
                else:
                    logger.warning(f"⚠️ Alert API returned status {response.status}")
                    await self._handle_api_error()
                    
        except aiohttp.ClientError as e:
            logger.warning(f"⚠️ HTTP error checking alert status: {e}")
            await self._handle_api_error()
            
        except json.JSONDecodeError as e:
            logger.warning(f"⚠️ JSON parse error from alert API: {e}")
            await self._handle_api_error()
            
        except Exception as e:
            logger.error(f"❌ Unexpected error checking alert status: {e}")
            await self._handle_api_error()
    
    async def _process_alert_data(self, data: Union[Dict[str, Any], List[Dict[str, Any]]]) -> None:
        """
        Process alert data and update the event status.
        Handles the new API format with activeAlerts array.
        
        Args:
            data: JSON response from alert API
        """
        try:
            air_alert = False
            
            if isinstance(data, list):
                # New format: list of region objects
                logger.debug(f"🔍 Processing list format with {len(data)} regions")
                
                # Look for Kyiv region (regionId = "31") in the list
                for region in data:
                    if isinstance(region, dict):
                        region_id = str(region.get("regionId", ""))
                        region_name = region.get("regionName", "")
                        
                        if region_id == "31":
                            logger.debug(f"🔍 Found Kyiv region: {region_name}")
                            
                            # Check activeAlerts array for AIR alerts
                            active_alerts = region.get("activeAlerts", [])
                            
                            if isinstance(active_alerts, list):
                                for alert in active_alerts:
                                    if isinstance(alert, dict):
                                        alert_type = alert.get("type", "")
                                        if alert_type == "AIR":
                                            air_alert = True
                                            logger.debug(f"🔍 Found AIR alert in activeAlerts")
                                            break
                            
                            # Exit loop once we found Kyiv region
                            break
                            
            elif isinstance(data, dict):
                # Legacy format: single region object
                logger.debug(f"🔍 Processing dict format")
                
                region_id = str(data.get("regionId", ""))
                if region_id == "31":
                    # Check activeAlerts array for AIR alerts
                    active_alerts = data.get("activeAlerts", [])
                    
                    if isinstance(active_alerts, list):
                        for alert in active_alerts:
                            if isinstance(alert, dict):
                                alert_type = alert.get("type", "")
                                if alert_type == "AIR":
                                    air_alert = True
                                    logger.debug(f"🔍 Found AIR alert in activeAlerts")
                                    break
                    
                    # Fallback to legacy "air" field if activeAlerts is not present
                    if not air_alert:
                        air_alert = data.get("air", False)
                        logger.debug(f"🔍 Using legacy 'air' field: {air_alert}")
                        
            else:
                logger.warning(f"⚠️ Unexpected data format: {type(data)}")
                logger.debug(f"🔍 Raw data: {data}")
                return
            
            # Update alert status if changed
            if air_alert != self.current_alert_status:
                self.current_alert_status = air_alert
                
                if air_alert:
                    self.alert_event.set()
                    logger.info("🚨 Kyiv AIR alert ON - forwarding enabled")
                else:
                    self.alert_event.clear()
                    logger.info("✅ Kyiv AIR alert OFF - forwarding disabled")
            else:
                # Log current status every few checks (to show it's working)
                status_emoji = "🚨" if air_alert else "✅"
                logger.debug(f"{status_emoji} Kyiv alert status unchanged: {'ON' if air_alert else 'OFF'}")
            
        except (KeyError, TypeError) as e:
            logger.warning(f"⚠️ Invalid alert data format: {e}")
            logger.debug(f"🔍 Raw data: {data}")
            await self._handle_api_error()
    
    async def _handle_api_error(self) -> None:
        """Handle API errors with exponential backoff."""
        self.consecutive_errors += 1
        
        if self.consecutive_errors >= self.max_consecutive_errors:
            logger.error(f"❌ {self.max_consecutive_errors} consecutive alert API errors - keeping current state")
            # Don't change alert status on persistent errors
            self.consecutive_errors = 0
        
        # Exponential backoff for next check
        backoff_time = min(2 ** self.consecutive_errors, 60)
        await asyncio.sleep(backoff_time)
    
    async def stop(self) -> None:
        """Stop the alert monitor and clean up resources."""
        if self.session:
            await self.session.close()
        logger.info("🧹 Alert monitor cleaned up")


class MediaGroupCollector:
    """
    Collects messages that belong to the same media group (album) and forwards them together.
    """
    
    def __init__(self, forwarder: 'TelegramForwarder'):
        """
        Initialize the media group collector.
        
        Args:
            forwarder: Reference to the TelegramForwarder instance
        """
        self.forwarder = forwarder
        self.media_groups: Dict[int, List[Message]] = defaultdict(list)
        self.group_timers: Dict[int, asyncio.Task] = {}
    
    async def handle_message(self, message: Message) -> None:
        """
        Handle a message that might be part of a media group.
        
        Args:
            message: The message to handle
        """
        grouped_id = message.grouped_id
        
        if grouped_id is None:
            # Single message, forward immediately
            await self.forwarder._forward_message(message)
        else:
            # Part of a media group, collect and wait for others
            await self._collect_media_group_message(message, grouped_id)
    
    async def _collect_media_group_message(self, message: Message, grouped_id: int) -> None:
        """
        Collect a message that's part of a media group.
        
        Args:
            message: The message to collect
            grouped_id: The grouped_id of the media group
        """
        # Add message to the group
        self.media_groups[grouped_id].append(message)
        
        # Cancel existing timer for this group if it exists
        if grouped_id in self.group_timers:
            self.group_timers[grouped_id].cancel()
        
        # Start a new timer to forward the group after timeout
        self.group_timers[grouped_id] = asyncio.create_task(
            self._forward_media_group_after_timeout(grouped_id)
        )
    
    async def _forward_media_group_after_timeout(self, grouped_id: int) -> None:
        """
        Wait for the timeout and then forward the collected media group.
        
        Args:
            grouped_id: The grouped_id of the media group to forward
        """
        try:
            await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
            
            # Get all messages in this group
            messages = self.media_groups.get(grouped_id, [])
            
            if messages:
                # Sort messages by date to maintain order
                messages.sort(key=lambda m: m.date)
                
                # Forward the entire media group
                await self.forwarder._forward_media_group(messages)
                
                # Clean up
                del self.media_groups[grouped_id]
                if grouped_id in self.group_timers:
                    del self.group_timers[grouped_id]
                    
        except asyncio.CancelledError:
            # Timer was cancelled, do nothing
            pass
        except Exception as e:
            logger.error(f"❌ Error forwarding media group {grouped_id}: {e}")


class TelegramForwarder:
    """
    A robust Telegram message forwarder that monitors multiple source channels
    and forwards messages to a target channel only during active air-raid alerts.
    """
    
    def __init__(self, api_id: int, api_hash: str, session_name: str, session_string: Optional[str] = None):
        """
        Initialize the Telegram forwarder.
        
        Args:
            api_id: Telegram API ID
            api_hash: Telegram API hash
            session_name: Name for the session file (used if session_string is not provided)
            session_string: Optional StringSession string (preferred for cloud deployment)
        """
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name
        self.session_string = session_string
        self.client: Optional[TelegramClient] = None
        self.source_entities: List[Union[Channel, InputChannel]] = []
        self.target_entity: Optional[Union[Channel, InputChannel]] = None
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self.alert_event = asyncio.Event()  # Controls forwarding based on alert status
        self.media_collector = MediaGroupCollector(self)
        
    async def initialize(self) -> bool:
        """
        Initialize the Telegram client and authenticate.
        
        Returns:
            bool: True if initialization successful, False otherwise
        """
        try:
            # Use StringSession if provided (preferred for cloud deployment)
            if self.session_string:
                session = StringSession(self.session_string)
                logger.info("🔐 Using StringSession for authentication")
            else:
                session = self.session_name
                logger.info(f"📁 Using session file: {self.session_name}")
            
            self.client = TelegramClient(session, self.api_id, self.api_hash)
            
            logger.info("🔄 Connecting to Telegram...")
            await self.client.connect()
            
            if not await self.client.is_user_authorized():
                logger.info("📱 Authorization required")
                await self._authorize()
            else:
                logger.info("✅ Already authorized")
            
            logger.info("🔄 Resolving channels...")
            await self._resolve_channels()
            
            logger.info("✅ Telegram initialization complete")
            return True
            
        except ApiIdInvalidError:
            logger.error("❌ Invalid API ID or API Hash")
            return False
        except Exception as e:
            logger.error(f"❌ Telegram initialization failed: {e}")
            return False
    
    async def _authorize(self) -> None:
        """Handle user authorization process."""
        try:
            phone = input("Enter your phone number (with country code): ")
            await self.client.send_code_request(phone)
            
            code = input("Enter the verification code: ")
            await self.client.sign_in(phone, code)
            
        except SessionPasswordNeededError:
            password = input("Enter your 2FA password: ")
            await self.client.sign_in(password=password)
        except PhoneCodeInvalidError:
            logger.error("❌ Invalid verification code")
            raise
        except PhoneNumberInvalidError:
            logger.error("❌ Invalid phone number")
            raise
    
    async def _resolve_channels(self) -> None:
        """Resolve all source channels and target channel."""
        # Resolve source channels
        for channel_name in SOURCE_CHANNELS:
            try:
                entity = await self.client.get_entity(channel_name)
                # Try to join the channel to ensure we receive updates
                try:
                    await self.client(JoinChannelRequest(entity))
                    logger.info(f"🤝 Joined channel: {channel_name}")
                except UserAlreadyParticipantError:
                    logger.debug(f"ℹ️ Already a participant of: {channel_name}")
                except Exception as join_err:
                    logger.warning(f"⚠️ Could not join {channel_name}: {join_err}")

                self.source_entities.append(entity)
                logger.info(f"✅ Resolved source channel: {channel_name}")
            except Exception as e:
                logger.error(f"❌ Failed to resolve source channel {channel_name}: {e}")
                continue
        
        if not self.source_entities:
            raise ValueError("No source channels could be resolved")
        
        # Resolve target channel
        try:
            self.target_entity = await self.client.get_entity(TARGET_CHANNEL)
            logger.info(f"✅ Resolved target channel: {TARGET_CHANNEL}")
        except Exception as e:
            logger.error(f"❌ Failed to resolve target channel {TARGET_CHANNEL}: {e}")
            raise
    
    def _truncate_message(self, text: str, max_length: int = 60) -> str:
        """Truncate message text for logging."""
        if not text:
            return "[no text]"
        
        # Clean up the text
        clean_text = text.strip().replace('\n', ' ').replace('\r', ' ')
        
        if len(clean_text) <= max_length:
            return clean_text
        
        return clean_text[:max_length] + "…"
    
    def _get_message_type(self, message: Message) -> str:
        """Get a human-readable message type."""
        if message.media:
            if isinstance(message.media, MessageMediaPhoto):
                return "photo"
            elif isinstance(message.media, MessageMediaDocument):
                if message.media.document.mime_type.startswith('video/'):
                    return "video"
                elif message.media.document.mime_type.startswith('audio/'):
                    return "audio"
                else:
                    return "document"
            elif isinstance(message.media, MessageMediaWebPage):
                return "webpage"
            else:
                return "media"
        elif message.text:
            return "text"
        else:
            return "unknown"
    
    async def _forward_message(self, message: Message) -> None:
        """
        Forward a single message to the target channel.
        
        Args:
            message: The message to forward
        """
        try:
            # Get channel info for logging
            channel = await message.get_chat()
            channel_name = getattr(channel, 'username', None) or str(channel.id)
            
            # Forward the message
            await self.client.forward_messages(
                entity=self.target_entity,
                messages=message,
                from_peer=message.peer_id
            )
            
            # Log the forwarded message
            msg_type = self._get_message_type(message)
            msg_preview = self._truncate_message(message.text or f"[{msg_type}]")
            
            logger.info(f"➡️ {channel_name}: {msg_preview}")
            
        except FloodWaitError as e:
            logger.warning(f"⏳ Rate limited, waiting {e.seconds} seconds...")
            await asyncio.sleep(e.seconds)
            # Retry after waiting
            await self._forward_message(message)
            
        except (BadMessageError, AuthKeyError, UnauthorizedError) as e:
            logger.error(f"❌ Authentication/message error: {e}")
            # These errors usually require re-authentication or reconnection
            await self._handle_reconnection()
            
        except RPCError as e:
            logger.error(f"❌ RPC error while forwarding: {e}")
            
        except Exception as e:
            logger.error(f"❌ Unexpected error while forwarding: {e}")
    
    async def _forward_media_group(self, messages: List[Message]) -> None:
        """
        Forward a media group (album) to the target channel.
        
        Args:
            messages: List of messages in the media group
        """
        try:
            if not messages:
                return
            
            # Get channel info for logging
            channel = await messages[0].get_chat()
            channel_name = getattr(channel, 'username', None) or str(channel.id)
            
            # Forward all messages in the group
            await self.client.forward_messages(
                entity=self.target_entity,
                messages=messages,
                from_peer=messages[0].peer_id
            )
            
            # Log the forwarded media group
            msg_types = [self._get_message_type(msg) for msg in messages]
            msg_preview = f"[album: {len(messages)} items - {', '.join(set(msg_types))}]"
            
            # If there's text in any message, include it
            text_content = None
            for msg in messages:
                if msg.text:
                    text_content = self._truncate_message(msg.text)
                    break
            
            if text_content:
                msg_preview = f"{msg_preview} {text_content}"
            
            logger.info(f"➡️ {channel_name}: {msg_preview}")
            
        except FloodWaitError as e:
            logger.warning(f"⏳ Rate limited, waiting {e.seconds} seconds...")
            await asyncio.sleep(e.seconds)
            # Retry after waiting
            await self._forward_media_group(messages)
            
        except (BadMessageError, AuthKeyError, UnauthorizedError) as e:
            logger.error(f"❌ Authentication/message error: {e}")
            # These errors usually require re-authentication or reconnection
            await self._handle_reconnection()
            
        except RPCError as e:
            logger.error(f"❌ RPC error while forwarding media group: {e}")
            
        except Exception as e:
            logger.error(f"❌ Unexpected error while forwarding media group: {e}")
    
    async def _handle_reconnection(self) -> None:
        """Handle client reconnection with exponential backoff."""
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            logger.error("❌ Maximum reconnection attempts reached")
            return
        
        self.reconnect_attempts += 1
        wait_time = min(2 ** self.reconnect_attempts, 60)  # Exponential backoff, max 60s
        
        logger.warning(f"🔄 Reconnecting in {wait_time} seconds (attempt {self.reconnect_attempts}/{self.max_reconnect_attempts})")
        await asyncio.sleep(wait_time)
        
        try:
            await self.client.disconnect()
            await self.client.connect()
            logger.info("✅ Reconnected successfully")
            self.reconnect_attempts = 0
        except Exception as e:
            logger.error(f"❌ Reconnection failed: {e}")
            await self._handle_reconnection()
    
    async def start_forwarding(self) -> None:
        """Start the message forwarding process."""
        logger.info("🚀 Starting message forwarder...")
        
        @self.client.on(events.NewMessage(chats=self.source_entities))
        async def handler(event: events.NewMessage.Event) -> None:
            """
            Handle new messages from source channels.
            Only forwards messages when alert_event is set (alert is active).
            In TEST_MODE, forwards all messages regardless of alert status.
            """
            try:
                # In test mode, always forward (bypass alert check)
                if TEST_MODE:
                    await self.media_collector.handle_message(event.message)
                    return
                
                # Check if alert is active before forwarding
                if not self.alert_event.is_set():
                    # Alert is not active, skip forwarding
                    return
                
                # Alert is active, handle the message (single or media group)
                await self.media_collector.handle_message(event.message)
                
            except Exception as e:
                logger.error(f"❌ Error in message handler: {e}")
        
        logger.info(f"👂 Listening to {len(self.source_entities)} channels...")
        logger.info(f"🎯 Will forward to channel: {TARGET_CHANNEL}")
        logger.info("📸 Media groups (albums) will be forwarded together")
        
        if TEST_MODE:
            logger.warning("🧪 TEST MODE ENABLED - All messages will be forwarded regardless of alert status!")
            logger.warning("🧪 Set TEST_MODE=false to enable normal alert filtering")
        else:
            logger.info("⚠️ Forwarding will only occur during active Kyiv AIR alerts")
        
        logger.info("✅ Forwarder is running. Press Ctrl+C to stop.")
        
        try:
            await self.client.run_until_disconnected()
        except KeyboardInterrupt:
            logger.info("🛑 Stopping forwarder...")
        except (ConnectionError, OSError, RPCError) as e:
            logger.error(f"❌ Connection/network error: {e}")
            await self._handle_reconnection()
            # Restart the forwarding process
            await self.start_forwarding()
        except Exception as e:
            logger.error(f"❌ Unexpected error: {e}")
            await self._handle_reconnection()
    
    async def cleanup(self) -> None:
        """Clean up resources."""
        if self.client:
            await self.client.disconnect()
            logger.info("🧹 Telegram client cleaned up")


async def main() -> None:
    """Main function to run the Telegram forwarder with alert monitoring."""
    # Validate required configuration
    if API_ID == 0 or not API_HASH:
        logger.error("❌ TG_API_ID and TG_API_HASH must be set as environment variables")
        logger.error("   Please set: TG_API_ID, TG_API_HASH, TG_TARGET_CHANNEL")
        logger.error("   Optional: TG_SESSION, TG_SESSION_STRING, TG_SOURCE_CHANNELS, ALERT_API_URL, TEST_MODE")
        return
    
    if TARGET_CHANNEL == 0:
        logger.error("❌ TG_TARGET_CHANNEL must be set as environment variable")
        return
    
    if not SOURCE_CHANNELS:
        logger.error("❌ TG_SOURCE_CHANNELS must be set as environment variable (comma-separated)")
        return
    
    # Use StringSession if provided, otherwise fall back to session file
    session_string = SESSION_STRING if SESSION_STRING else None
    if not session_string:
        logger.info("ℹ️  TG_SESSION_STRING not set, will use session file (not recommended for cloud)")
    
    forwarder = TelegramForwarder(API_ID, API_HASH, SESSION, session_string=session_string)
    alert_monitor = None
    
    try:
        # Initialize Telegram client
        if not await forwarder.initialize():
            logger.error("❌ Failed to initialize Telegram client")
            return
        
        # Create alert monitor
        alert_monitor = AlertMonitor(forwarder.alert_event)
        
        # Start both tasks concurrently
        logger.info("🚀 Starting alert monitor and message forwarder...")
        
        tasks = [
            asyncio.create_task(alert_monitor.start()),
            asyncio.create_task(forwarder.start_forwarding())
        ]
        
        # Wait for either task to complete (or fail)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        
        # Cancel remaining tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Check for exceptions in completed tasks
        for task in done:
            try:
                await task
            except Exception as e:
                logger.error(f"❌ Task failed: {e}")
                
    except KeyboardInterrupt:
        logger.info("👋 Shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        sys.exit(1)
    finally:
        # Clean up resources
        await forwarder.cleanup()
        if alert_monitor:
            await alert_monitor.stop()


if __name__ == "__main__":
    # Handle event loop for different Python versions
    if sys.version_info >= (3, 10):
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
    else:
        loop = asyncio.get_event_loop()
        try:
            loop.run_until_complete(main())
        except KeyboardInterrupt:
            pass
        finally:
            loop.close()
