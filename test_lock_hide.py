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
from app.auth import get_current_user
from app.config import get_settings

async def main():
    print("=== E2E Integration Test for Folder Lock & Hide ===\n")
    
    unique_id = uuid.uuid4().hex[:6]
    email = f"lock_test_{unique_id}@yopmail.com"
    name = f"Lock Test User {unique_id}"
    password = "password123"
    
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=30.0) as client:
        # 1. Register User
        print("1. Registering user...")
        resp = await client.post("/api/auth/register", json={"email": email, "password": password, "name": name})
        assert resp.status_code == 201
        data = resp.json()
        token = data["token"]
        user_id = data["user"]["id"]
        headers = {"Authorization": f"Bearer {token}"}
        
        # Wait for registration sync
        await asyncio.sleep(3)
        
        # 2. Create parent folder and child folder
        print("2. Creating parent folder...")
        resp = await client.post("/api/folders", json={"name": "SecureFolder", "type": "folder", "parentId": None}, headers=headers)
        assert resp.status_code == 201
        folder_id = resp.json()["id"]
        
        print("3. Creating file inside parent folder...")
        resp = await client.post(
            "/api/files/upload",
            data={"parentId": folder_id},
            files={"file": ("secret.txt", b"Top Secret Data", "text/plain")},
            headers=headers
        )
        assert resp.status_code == 201
        file_id = resp.json()["id"]
        
        # 3. Lock Folder SecureFolder
        print("4. Locking SecureFolder with password 'secret123'...")
        resp = await client.post(
            f"/api/folders/{folder_id}/lock",
            json={"password": "secret123"},
            headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["isLocked"] is True
        
        # 4. List folders without header
        print("5. Listing items without X-Folder-Passwords header...")
        resp = await client.get("/api/folders", headers=headers)
        assert resp.status_code == 200
        items = resp.json()
        
        # SecureFolder itself should be visible
        secure_folder_listed = any(i["id"] == folder_id for i in items)
        # secret.txt inside SecureFolder should be filtered out
        secret_file_listed = any(i["id"] == file_id for i in items)
        
        print(f"   SecureFolder visible: {secure_folder_listed}")
        print(f"   secret.txt visible (should be False): {secret_file_listed}")
        assert secure_folder_listed is True
        assert secret_file_listed is False
        
        # 5. Challenge Unlock folder
        print("6. Challenging unlock with wrong password...")
        resp = await client.post(f"/api/folders/{folder_id}/unlock", json={"password": "wrongpassword"}, headers=headers)
        assert resp.status_code == 400
        
        print("7. Challenging unlock with correct password...")
        resp = await client.post(f"/api/folders/{folder_id}/unlock", json={"password": "secret123"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["message"] == "Success"
        
        # 6. List folders WITH correct X-Folder-Passwords header
        print("8. Listing items with correct X-Folder-Passwords header...")
        pw_headers = headers.copy()
        pw_headers["X-Folder-Passwords"] = f'{{"{folder_id}": "secret123"}}'
        resp = await client.get("/api/folders", headers=pw_headers)
        assert resp.status_code == 200
        items = resp.json()
        
        secret_file_listed = any(i["id"] == file_id for i in items)
        print(f"   secret.txt visible with header: {secret_file_listed}")
        assert secret_file_listed is True
        
        # 7. Try to upload to locked folder without password
        print("9. Trying to upload file to locked folder without password...")
        resp = await client.post(
            "/api/files/upload",
            data={"parentId": folder_id},
            files={"file": ("fail.txt", b"should fail", "text/plain")},
            headers=headers
        )
        assert resp.status_code == 403
        
        # 8. Try to upload to locked folder WITH password
        print("10. Uploading file to locked folder WITH password header...")
        resp = await client.post(
            "/api/files/upload",
            data={"parentId": folder_id},
            files={"file": ("success.txt", b"should succeed", "text/plain")},
            headers=pw_headers
        )
        assert resp.status_code == 201
        
        # 9. Test View File block
        print("11. Viewing secret.txt file without password header/query param...")
        resp = await client.get(f"/api/files/{file_id}/view", headers=headers)
        assert resp.status_code == 403
        
        print("12. Viewing secret.txt file WITH query param passwords...")
        resp = await client.get(f"/api/files/{file_id}/view?passwords={{\"{folder_id}\":\"secret123\"}}&token={token}")
        assert resp.status_code == 200
        assert resp.content == b"Top Secret Data"
        
        # 10. Test Hide/Unhide
        print("13. Hiding secret.txt file...")
        resp = await client.post(f"/api/folders/{file_id}/hide", headers=pw_headers)
        assert resp.status_code == 200
        assert resp.json()["isHidden"] is True
        
        # 11. Disable lock
        print("14. Permanently disabling lock on SecureFolder...")
        resp = await client.post(f"/api/folders/{folder_id}/disable-lock", json={"password": "secret123"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["isLocked"] is False
        
        # Clean up database test user and items
        print("15. Cleaning up DB test user and items...")
        await init_db()
        user_doc = await User.get(user_id)
        if user_doc:
            await user_doc.delete()
        user_items = await FileSystemItem.find(FileSystemItem.user_id == user_id).to_list()
        for item in user_items:
            await item.delete()
            
        print("\nAll Lock & Hide integration tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(main())
