import sys
import os
from dotenv import load_dotenv

# Load env variables from .env
load_dotenv()

# Add backend directory to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.b2 import get_b2_client
from app.config import get_settings

def main():
    settings = get_settings()
    print("Testing Backblaze B2 S3 Connection...")
    print(f"B2_ENDPOINT: {settings.B2_ENDPOINT}")
    print(f"B2_BUCKET: {settings.B2_BUCKET}")
    print(f"B2_KEY_ID: {settings.B2_KEY_ID[:4]}...{settings.B2_KEY_ID[-4:] if len(settings.B2_KEY_ID) > 8 else ''}")

    try:
        client = get_b2_client()
        # List objects in the bucket (max 5)
        response = client.list_objects_v2(Bucket=settings.B2_BUCKET, MaxKeys=5)
        print("Connection Successful!")
        contents = response.get("Contents", [])
        print(f"Found {len(contents)} objects in the bucket:")
        for obj in contents:
            print(f"  - {obj['Key']} ({obj['Size']} bytes)")
    except Exception as e:
        print(f"Connection Failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
