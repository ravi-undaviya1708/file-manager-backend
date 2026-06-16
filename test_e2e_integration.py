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
    b2_client = get_b2_client()
    
    unique_id = uuid.uuid4().hex[:6]
    email = f"e2e_{unique_id}@yopmail.com"
    name = f"E2E User {unique_id}"
    password = "password123"
    
    print(f"=== E2E Integration Test for B2 ===\n")
    
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=30.0) as client:
        # 1. Register User
        print("1. Registering user...")
        resp = await client.post("/api/auth/register", json={"email": email, "password": password, "name": name})
        assert resp.status_code == 201
        data = resp.json()
        token = data["token"]
        user_id = data["user"]["id"]
        print(f"   Registered user {user_id}")
        
        headers = {"Authorization": f"Bearer {token}"}
        
        # Wait for registration sync
        await asyncio.sleep(4)
        
        # Clear seeded B2 objects to start from a clean state for user
        print("   Clearing B2 items to start fresh...")
        res = b2_client.list_objects_v2(Bucket=settings.B2_BUCKET, Prefix=f"{user_id}/")
        for obj in res.get("Contents", []):
            b2_client.delete_object(Bucket=settings.B2_BUCKET, Key=obj["Key"])
            
        # Re-create root keep
        b2_client.put_object(Bucket=settings.B2_BUCKET, Key=f"{user_id}/.keep", Body=b"")
        
        # 2. Create Folder
        print("2. Creating folder 'TestFolder' via API...")
        resp = await client.post("/api/folders", json={"name": "TestFolder", "type": "folder", "parentId": None}, headers=headers)
        assert resp.status_code == 201
        folder_id = resp.json()["id"]
        
        # Check B2
        res = b2_client.list_objects_v2(Bucket=settings.B2_BUCKET, Prefix=f"{user_id}/")
        keys = [obj["Key"] for obj in res.get("Contents", [])]
        print(f"   B2 Keys: {keys}")
        assert f"{user_id}/TestFolder/.keep" in keys
        
        # 3. Upload File
        print("3. Uploading file 'hello.txt' inside 'TestFolder' via API...")
        file_content = b"Hello from E2E integration test!"
        files = {"file": ("hello.txt", file_content, "text/plain")}
        resp = await client.post("/api/files/upload", data={"parentId": folder_id}, files=files, headers=headers)
        assert resp.status_code == 201
        file_id = resp.json()["id"]
        
        # Check B2
        res = b2_client.list_objects_v2(Bucket=settings.B2_BUCKET, Prefix=f"{user_id}/")
        keys = [obj["Key"] for obj in res.get("Contents", [])]
        print(f"   B2 Keys: {keys}")
        assert f"{user_id}/TestFolder/hello.txt" in keys
        
        # Verify file contents in B2
        obj_res = b2_client.get_object(Bucket=settings.B2_BUCKET, Key=f"{user_id}/TestFolder/hello.txt")
        body = obj_res["Body"].read()
        print(f"   File Content in B2: '{body.decode()}'")
        assert body == file_content
        
        # 4. Rename Folder
        print("4. Renaming 'TestFolder' to 'RenamedFolder' via API...")
        resp = await client.patch(f"/api/folders/{folder_id}/rename", json={"name": "RenamedFolder"}, headers=headers)
        assert resp.status_code == 200
        
        # Check B2 (wait a bit for the operation to propagate if needed, though it is synchronous)
        res = b2_client.list_objects_v2(Bucket=settings.B2_BUCKET, Prefix=f"{user_id}/")
        keys = [obj["Key"] for obj in res.get("Contents", [])]
        print(f"   B2 Keys: {keys}")
        assert f"{user_id}/RenamedFolder/.keep" in keys
        assert f"{user_id}/RenamedFolder/hello.txt" in keys
        assert f"{user_id}/TestFolder/hello.txt" not in keys
        
        # 5. Move File
        print("5. Creating folder 'DestFolder' and moving 'hello.txt' into it...")
        resp = await client.post("/api/folders", json={"name": "DestFolder", "type": "folder", "parentId": None}, headers=headers)
        assert resp.status_code == 201
        dest_folder_id = resp.json()["id"]
        
        resp = await client.patch(f"/api/folders/{file_id}/move", json={"targetParentId": dest_folder_id}, headers=headers)
        assert resp.status_code == 200
        
        # Check B2
        res = b2_client.list_objects_v2(Bucket=settings.B2_BUCKET, Prefix=f"{user_id}/")
        keys = [obj["Key"] for obj in res.get("Contents", [])]
        print(f"   B2 Keys: {keys}")
        assert f"{user_id}/DestFolder/hello.txt" in keys
        assert f"{user_id}/RenamedFolder/hello.txt" not in keys
        
        # 6. Delete Folder (hard-delete)
        print("6. Soft-deleting 'RenamedFolder' then permanently deleting it...")
        # Soft delete
        resp = await client.delete(f"/api/folders/{folder_id}", headers=headers)
        assert resp.status_code == 200
        
        # Permanent delete
        resp = await client.delete(f"/api/folders/{folder_id}?permanent=true", headers=headers)
        assert resp.status_code == 200
        
        # Check B2
        res = b2_client.list_objects_v2(Bucket=settings.B2_BUCKET, Prefix=f"{user_id}/")
        keys = [obj["Key"] for obj in res.get("Contents", [])]
        print(f"   B2 Keys: {keys}")
        assert f"{user_id}/RenamedFolder/.keep" not in keys
        assert f"{user_id}/RenamedFolder/hello.txt" not in keys
        
        # Clean up database test user and items
        print("7. Cleaning up test user and remaining files in DB...")
        await init_db()
        user_doc = await User.get(user_id)
        if user_doc:
            await user_doc.delete()
        user_items = await FileSystemItem.find(FileSystemItem.user_id == user_id).to_list()
        for item in user_items:
            await item.delete()
            
        # Clean up remaining B2 objects
        print("8. Cleaning up B2 bucket...")
        res = b2_client.list_objects_v2(Bucket=settings.B2_BUCKET, Prefix=f"{user_id}/")
        for obj in res.get("Contents", []):
            b2_client.delete_object(Bucket=settings.B2_BUCKET, Key=obj["Key"])
            
        print("\nAll E2E Integration tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(main())
