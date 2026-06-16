import asyncio
import sys
import os
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.database import init_db
from app.models import FileSystemItem

async def main():
    await init_db()
    items = await FileSystemItem.find_all().to_list()
    print(f"Total items in DB: {len(items)}")
    for item in items:
        print(f"Item: ID={item.id}, Name='{item.name}', Type={item.type}, UserID='{item.user_id}', ParentID='{item.parent_id}'")

if __name__ == "__main__":
    asyncio.run(main())
