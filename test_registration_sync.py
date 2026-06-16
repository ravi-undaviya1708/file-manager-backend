import asyncio
import sys
import os
import httpx
from dotenv import load_dotenv
import uuid

load_dotenv()
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.database import init_db
from app.models import User, FileSystemItem
from app.b2 import get_b2_client
from app.config import get_settings

async def main():
    settings = get_settings()
    
    # Generate unique email for registration
    unique_id = uuid.uuid4().hex[:6]
    email = f"testuser_{unique_id}@yopmail.com"
    name = f"Test User {unique_id}"
    password = "password123"
    
    print(f"Registering user: {email}...")
    
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        # Register user
        register_payload = {
            "email": email,
            "password": password,
            "name": name
        }
        response = await client.post("/api/auth/register", json=register_payload)
        if response.status_code != 201:
            print(f"Registration failed with code {response.status_code}: {response.text}")
            return
        
        reg_data = response.json()
        user_id = reg_data["user"]["id"]
        print(f"Registered successfully! User ID: {user_id}")
        
        # Wait a few seconds for background tasks to complete the sync
        print("Waiting 5 seconds for background tasks to complete B2 sync...")
        await asyncio.sleep(5)
        
        # Verify MongoDB items
        print("Verifying seeded items in MongoDB...")
        await init_db()
        items = await FileSystemItem.find(FileSystemItem.user_id == user_id).to_list()
        print(f"Found {len(items)} items in MongoDB for this user.")
        
        # Verify B2 objects
        print("Verifying objects in Backblaze B2...")
        b2_client = get_b2_client()
        b2_response = b2_client.list_objects_v2(Bucket=settings.B2_BUCKET, Prefix=f"{user_id}/")
        contents = b2_response.get("Contents", [])
        print(f"Found {len(contents)} objects in the B2 bucket for user {user_id}:")
        for obj in contents:
            print(f"  - {obj['Key']} ({obj['Size']} bytes)")
            
        # Clean up database items for this test user
        print("Cleaning up test user data...")
        user_doc = await User.get(user_id)
        if user_doc:
            await user_doc.delete()
        for item in items:
            await item.delete()
            
        # Clean up B2 objects
        print("Cleaning up B2 test objects...")
        for obj in contents:
            b2_client.delete_object(Bucket=settings.B2_BUCKET, Key=obj["Key"])
        print("Cleanup complete.")

if __name__ == "__main__":
    asyncio.run(main())
