import asyncio
import sys
import os
from dotenv import load_dotenv
import uuid

load_dotenv()
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.database import init_db
from app.models import User, FileSystemItem
from app.b2 import get_item_path, create_b2_object, get_b2_client
from app.config import get_settings
from app.seed import seed_user_data

async def main():
    await init_db()
    settings = get_settings()
    
    unique_id = uuid.uuid4().hex[:6]
    email = f"debug_{unique_id}@yopmail.com"
    user = User(email=email, name="Debug User")
    await user.insert()
    user_id = str(user.id)
    print(f"Created Debug User: {user_id}")
    
    from app.b2 import get_user_b2_prefix
    b2_prefix = await get_user_b2_prefix(user_id)
    print(f"B2 Prefix: {b2_prefix}")
    
    await seed_user_data(user_id)
    items = await FileSystemItem.find(FileSystemItem.user_id == user_id).to_list()
    print(f"Seeded {len(items)} items in MongoDB.")
    
    # Run sync inline with detailed print
    print("\nRunning sync inline with debugging...")
    root_key = f"{b2_prefix}/.keep"
    create_b2_object(root_key, b"")
    
    for item in items:
        try:
            print(f"Syncing item '{item.name}' (type={item.type}, id={item.id})...")
            path = await get_item_path(item, user_id)
            key = f"{b2_prefix}/{path}"
            print(f"  Resolved path: '{path}' -> key: '{key}'")
            
            if item.type == "folder":
                res = create_b2_object(f"{key}/.keep", b"")
                print(f"  Created folder B2 object: {res}")
            else:
                meta = (
                    f"Placeholder for existing file synced during B2 integration.\n"
                    f"Name: {item.name}\n"
                    f"Original Size: {item.size or 0} bytes\n"
                    f"Created: {item.created_at.isoformat() if item.created_at else ''}\n"
                ).encode("utf-8")
                res = create_b2_object(key, meta)
                print(f"  Created file B2 object: {res}")
        except Exception as e:
            print(f"  ERROR syncing item '{item.name}': {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

    # Verify B2 objects
    print("\nVerifying final objects in B2...")
    b2_client = get_b2_client()
    b2_response = b2_client.list_objects_v2(Bucket=settings.B2_BUCKET, Prefix=f"{b2_prefix}/")
    contents = b2_response.get("Contents", [])
    print(f"Found {len(contents)} objects in the B2 bucket:")
    for obj in contents:
        print(f"  - {obj['Key']}")
        
    # Cleanup
    print("\nCleaning up...")
    await user.delete()
    for item in items:
        await item.delete()
    for obj in contents:
        b2_client.delete_object(Bucket=settings.B2_BUCKET, Key=obj["Key"])
    print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
