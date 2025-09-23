import os
import json
import asyncio
import time
import urllib.request
import urllib.parse
import base64
import requests
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
PHONE_NUMBER = os.getenv('PHONE_NUMBER')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_REPO = os.getenv('GITHUB_REPO')
GITHUB_FILE_PATH = os.getenv('GITHUB_FILE_PATH', 'users.json')
PORT = int(os.getenv('PORT', 3000))
RENDER_EXTERNAL_URL = os.getenv('RENDER_EXTERNAL_URL')

if not API_ID or not API_HASH or not PHONE_NUMBER:
    logger.error("Missing required environment variables: API_ID, API_HASH, PHONE_NUMBER")
    exit(1)

API_ID = int(API_ID)

client = TelegramClient('session_name', API_ID, API_HASH)

last_config_check = 0
current_config = {}
monitored_entities = set()
last_forward_times = {}

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Telegram Monitor is running')
    
    def log_message(self, format, *args):
        pass

def start_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    server.serve_forever()

def create_default_config():
    """Create a default configuration if none exists"""
    default_config = {
        "monitoring_active": False,
        "keywords": ["urgent", "important", "alert"],
        "cooldown": 2,
        "destination_group": "",
        "channels": {},
        "groups": {},
        "statistics": {
            "messages_forwarded": 0,
            "keywords_triggered": 0,
            "last_reset": datetime.now().isoformat()
        }
    }
    
    try:
        with open('users.json', 'w') as f:
            json.dump(default_config, f, indent=2)
        logger.info("Created default users.json configuration file")
        return default_config
    except Exception as e:
        logger.error(f"Could not create default config: {e}")
        return default_config

def get_config_from_github():
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {}
    
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
        
        request = urllib.request.Request(url)
        request.add_header('Authorization', f'token {GITHUB_TOKEN}')
        request.add_header('User-Agent', 'TelegramMonitor')
        
        with urllib.request.urlopen(request) as response:
            if response.status == 200:
                file_data = json.loads(response.read().decode())
                content = base64.b64decode(file_data['content']).decode('utf-8')
                config = json.loads(content)
                logger.info("Config loaded from GitHub")
                return config
            else:
                logger.warning(f"GitHub API returned status: {response.status}")
                return {}
    except Exception as e:
        logger.warning(f"Could not load from GitHub (using local config): {e}")
        return {}

def get_config_from_file():
    try:
        with open('users.json', 'r') as f:
            config = json.load(f)
            logger.info("Config loaded from local file")
            return config
    except FileNotFoundError:
        logger.info("Local users.json not found, creating default configuration")
        return create_default_config()
    except Exception as e:
        logger.error(f"Error reading local config: {e}")
        return create_default_config()

def load_config():
    config = get_config_from_github()
    
    if config:
        logger.info("Using config from GitHub")
        try:
            with open('users.json', 'w') as f:
                json.dump(config, f, indent=2)
            logger.info("GitHub config synced to local file")
        except Exception as e:
            logger.warning(f"Could not sync GitHub config to local file: {e}")
    else:
        logger.info("Using local configuration")
        config = get_config_from_file()
    
    if not isinstance(config.get('monitoring_active'), bool):
        config['monitoring_active'] = False
        logger.warning("Fixed monitoring_active field in config")
    
    if not isinstance(config.get('keywords'), list):
        config['keywords'] = ["urgent", "important"]
        logger.warning("Fixed keywords field in config")
    
    return config

def should_forward_message(text, keywords):
    if not text or not keywords:
        return False
    
    text_lower = text.lower()
    for keyword in keywords:
        if keyword.lower() in text_lower:
            return True
    return False

def check_cooldown(chat_id, cooldown_minutes):
    if chat_id not in last_forward_times:
        return True
    
    last_time = last_forward_times[chat_id]
    current_time = time.time()
    time_diff = (current_time - last_time) / 60
    
    return time_diff >= cooldown_minutes

async def get_entity_by_name(name):
    try:
        if name.startswith('@'):
            name = name[1:]
        entity = await client.get_entity(name)
        return entity
    except Exception as e:
        logger.error(f"Could not get entity {name}: {e}")
        return None

async def forward_message_to_group(message, destination_group):
    try:
        dest_entity = await get_entity_by_name(destination_group)
        if not dest_entity:
            logger.error(f"Could not find destination group: {destination_group}")
            return False
        
        await client.forward_messages(dest_entity, message)
        logger.info(f"Message forwarded to {destination_group}")
        return True
    except Exception as e:
        logger.error(f"Error forwarding message: {e}")
        return False

async def update_statistics(forwarded=False, keyword_triggered=False):
    try:
        config = get_config_from_file()
        if 'statistics' not in config:
            config['statistics'] = {
                'messages_forwarded': 0,
                'keywords_triggered': 0,
                'last_reset': datetime.now().isoformat()
            }
        
        if forwarded:
            config['statistics']['messages_forwarded'] += 1
        if keyword_triggered:
            config['statistics']['keywords_triggered'] += 1
        
        with open('users.json', 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.error(f"Error updating statistics: {e}")

async def setup_monitoring():
    global monitored_entities, current_config
    
    config = load_config()
    current_config = config
    
    if not config.get('monitoring_active', False):
        logger.warning("⚠️  Monitoring is INACTIVE - Set 'monitoring_active': true in users.json to enable")
        logger.info("Current config status:")
        logger.info(f"  - Keywords: {config.get('keywords', [])}")
        logger.info(f"  - Destination: {config.get('destination_group', 'Not set')}")
        logger.info(f"  - Channels: {len(config.get('channels', {}))}")
        logger.info(f"  - Groups: {len(config.get('groups', {}))}")
        return
    
    new_entities = set()
    
    channels = config.get('channels', {})
    for tag, channel_data in channels.items():
        entity = await get_entity_by_name(channel_data['name'])
        if entity:
            new_entities.add(entity.id)
            logger.info(f"✅ Monitoring channel: {channel_data['name']}")
        else:
            logger.error(f"❌ Could not access channel: {channel_data['name']}")
    
    groups = config.get('groups', {})
    for tag, group_data in groups.items():
        entity = await get_entity_by_name(group_data['name'])
        if entity:
            new_entities.add(entity.id)
            logger.info(f"✅ Monitoring group: {group_data['name']}")
        else:
            logger.error(f"❌ Could not access group: {group_data['name']}")
    
    monitored_entities = new_entities
    
    if monitored_entities:
        logger.info(f"🚀 MONITORING ACTIVE - Watching {len(monitored_entities)} entities for keywords: {config.get('keywords', [])}")
    else:
        logger.warning("⚠️  No entities are being monitored. Check your channel/group names in users.json")

@client.on(events.NewMessage)
async def handle_new_message(event):
    global last_config_check, current_config
    
    current_time = time.time()
    if current_time - last_config_check > 30:
        await setup_monitoring()
        last_config_check = current_time
    
    if not current_config.get('monitoring_active', False):
        return
    
    if event.chat_id not in monitored_entities:
        return
    
    message_text = event.message.message
    if not message_text:
        return
    
    keywords = current_config.get('keywords', [])
    if not should_forward_message(message_text, keywords):
        return
    
    cooldown = current_config.get('cooldown', 2)
    if not check_cooldown(event.chat_id, cooldown):
        logger.info(f"⏳ Cooldown active for chat {event.chat_id}")
        return
    
    destination = current_config.get('destination_group')
    if not destination:
        logger.error("❌ No destination group configured in users.json")
        return
    
    await update_statistics(keyword_triggered=True)
    
    success = await forward_message_to_group(event.message, destination)
    if success:
        last_forward_times[event.chat_id] = current_time
        await update_statistics(forwarded=True)
        logger.info(f"📬 Forwarded message from {event.chat_id}")

async def self_ping():
    if RENDER_EXTERNAL_URL:
        try:
            response = requests.get(RENDER_EXTERNAL_URL, timeout=30)
            if response.status_code == 200:
                logger.info("✅ Self-ping successful")
            else:
                logger.warning(f"❌ Self-ping failed: {response.status_code}")
        except Exception as e:
            logger.warning(f"❌ Self-ping error: {str(e)}")
    else:
        logger.info("💓 Keep-alive ping (no external URL configured)")

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        await self_ping()

async def start_client():
    try:
        await client.start(phone=PHONE_NUMBER)
        
        if not await client.is_user_authorized():
            logger.error("❌ Client not authorized! You need to authenticate locally first.")
            logger.error("Run this script locally, authenticate, then upload the session file to your server.")
            return False
        
        logger.info("✅ Client authorized successfully")
        
        me = await client.get_me()
        logger.info(f"👤 Logged in as {me.first_name} ({me.username})")
        
        await setup_monitoring()
        
        logger.info("🔄 Starting message monitoring loop...")
        
        asyncio.create_task(keep_alive())
        
        await client.run_until_disconnected()
        return True
        
    except Exception as e:
        logger.error(f"❌ Error in start_client: {e}")
        return False

def main():
    try:
        logger.info("🚀 Starting Telegram Monitor Bot")
        logger.info(f"📡 Health check server starting on port {PORT}")
        
        health_thread = threading.Thread(target=start_health_server, daemon=True)
        health_thread.start()
        
        asyncio.run(start_client())
    except KeyboardInterrupt:
        logger.info("⚠️  Bot stopped by user")
    except Exception as e:
        logger.error(f"💥 Fatal error: {e}")

if __name__ == '__main__':
    main()
