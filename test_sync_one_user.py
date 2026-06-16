import asyncio
import sys
import os
from dotenv import load_dotenv

# Load env variables
load_dotenv()

# Add backend directory to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.database import init_db
from app.b2 import check_and_sync_user, get_b2_client
from app.config import get_settings

async def main():
    settings = get_settings()
    print("Initializing Database...")
    await init_db()
    
    user_id = "6a2fb48fabbf325296e9ca0a"  # Demo User
    print(f"Triggering sync for user {user_id}...")
    await check_and_sync_user(user_id)
    
    print("Checking Backblaze B2 objects...")
    client = get_b2_client()
    response = client.list_objects_v2(Bucket=settings.B2_BUCKET, Prefix=f"{user_id}/")
    contents = response.get("Contents", [])
    print(f"Found {len(contents)} objects in the bucket for prefix {user_id}/:")
    for obj in contents:
        print(f"  - {obj['Key']} ({obj['Size']} bytes)")

if __name__ == "__main__":
    asyncio.run(main())
