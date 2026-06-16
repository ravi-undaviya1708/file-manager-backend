import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from app.config import get_settings

async def main():
    settings = get_settings()
    print("Connecting to database:", settings.MONGODB_URL)
    client = AsyncIOMotorClient(settings.MONGODB_URL)
    db = client[settings.MONGODB_DB_NAME]
    
    # List collections
    collections = await db.list_collection_names()
    print("Collections in database:", collections)
    
    # Find users
    user_col = "User" if "User" in collections else "users" if "users" in collections else None
    if user_col:
        users = await db[user_col].find().to_list(100)
        print(f"Found {len(users)} users:")
        for u in users:
            print(f"  - ID: {u['_id']}, Email: {u.get('email') or u.get('email_address')}, Name: {u.get('name')}")
    else:
        print("No User collection found.")
        
    item_col = "FileSystemItem" if "FileSystemItem" in collections else "file_system_items" if "file_system_items" in collections else None
    if item_col:
        items_count = await db[item_col].count_documents({})
        print(f"Total FileSystemItems: {items_count}")
    else:
        print("No FileSystemItem collection found.")

if __name__ == "__main__":
    asyncio.run(main())
