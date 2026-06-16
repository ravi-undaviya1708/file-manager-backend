# File Manager Backend

A FastAPI backend for the File Manager application, powered by **MongoDB Atlas**.

## Tech Stack

- **FastAPI** — Async web framework
- **Motor** — Async MongoDB driver
- **Beanie** — Async ODM (Object Document Mapper) built on Motor + Pydantic
- **Pydantic v2** — Request/response validation
- **Uvicorn** — ASGI server

## Quick Start

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the development server
uvicorn app.main:app --reload --port 8000
```

The API will be available at `http://localhost:8000`.

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **Health Check**: http://localhost:8000/health

## Connecting the Frontend

Set the following environment variable in the Next.js frontend (`.env.local`):

```env
NEXT_PUBLIC_API_URL=http://localhost:8000
```

## Environment Variables

| Variable          | Description                    | Default                      |
| ----------------- | ------------------------------ | ---------------------------- |
| `MONGODB_URL`     | MongoDB connection string      | `mongodb://localhost:27017`  |
| `MONGODB_DB_NAME` | Database name                  | `mongo_db_name`               |
| `CORS_ORIGINS`    | Comma-separated allowed origins| `http://localhost:3000,...`   |
| `APP_ENV`         | Environment (`development`)    | `development`                |
| `JWT_SECRET_KEY`    | JWT signing secret key         | `jwtsecratekey` |
| `JWT_ALGORITHM`     | JWT signing algorithm          | `HS256`                      |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Access token expiration    | `10080`                      |
| `B2_KEY_ID`         | Backblaze b2 key id            | `b2-key-id`      |
| `B2_APPLICATION_KEY`| Backblaze b2 application key   | `b2-application-key`|
| `B2_BUCKET`         | Backblaze b2 bucket            | `b2-bucket`            |
| `B2_ENDPOINT`       | Backblaze b2 endpoint          | `b2-endpoint`|

## API Endpoints

| Method   | Endpoint                          | Description                        |
| -------- | --------------------------------- | ---------------------------------- |
| `GET`    | `/api/folders`                    | List all files and folders         |
| `POST`   | `/api/folders`                    | Create a new folder                |
| `GET`    | `/api/folders/{id}`               | Get a single item by ID            |
| `PATCH`  | `/api/folders/{id}/rename`        | Rename a file or folder            |
| `PATCH`  | `/api/folders/{id}/move`          | Move item to a different folder    |
| `PATCH`  | `/api/folders/{id}/star`          | Toggle starred status              |
| `DELETE` | `/api/folders/{id}`               | Soft-delete (or permanent delete)  |
| `PATCH`  | `/api/folders/{id}/restore`       | Restore from bin                   |
| `POST`   | `/api/folders/{id}/duplicate`     | Duplicate an item                  |
| `POST`   | `/api/files/upload`               | Upload a file                      |
| `GET`    | `/health`                         | Health check                       |

## Project Structure

```
file-manager-backend/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI app entry point
│   ├── config.py         # Settings from .env
│   ├── database.py       # Motor client & Beanie init
│   ├── models.py         # Beanie document models
│   ├── schemas.py        # Pydantic request/response schemas
│   ├── crud.py           # Database operations
│   ├── routes.py         # API route handlers
│   └── seed.py           # Sample data seeder
├── requirements.txt
├── .env
├── .gitignore
└── README.md
```
