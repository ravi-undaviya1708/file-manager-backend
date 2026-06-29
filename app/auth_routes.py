"""Router for authentication and Google Sign-In endpoints."""

from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, status, BackgroundTasks, Form, File, UploadFile

from app.auth import (
    hash_password,
    verify_password,
    create_access_token,
    verify_google_token,
    get_current_user,
)
from app.models import User
from app.schemas import (
    UserRegisterRequest,
    UserLoginRequest,
    GoogleLoginRequest,
    AuthTokenResponse,
    UserResponse,
    ErrorResponse,
)
from app.seed import seed_user_data

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


def _to_user_response(user: User) -> UserResponse:
    """Helper to convert Beanie User document to UserResponse schema."""
    avatar_url = user.avatar_url
    if avatar_url and "backblazeb2.com" in avatar_url:
        from app.b2 import generate_presigned_url
        from app.config import get_settings
        bucket = get_settings().B2_BUCKET
        prefix = f"/file/{bucket}/"
        if prefix in avatar_url:
            key = avatar_url.split(prefix)[-1]
            presigned = generate_presigned_url(key)
            if presigned:
                avatar_url = presigned

    return UserResponse(
        id=str(user.id),
        name=user.name,
        email=user.email,
        avatarUrl=avatar_url,
        createdAt=user.created_at.isoformat() if user.created_at else "",
        isAdmin=user.is_admin,
        storageLimitBytes=user.storage_limit_bytes,
        pricingPlan=user.pricing_plan,
    )


@router.post(
    "/register",
    response_model=AuthTokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
    responses={409: {"model": ErrorResponse}, 400: {"model": ErrorResponse}},
)
async def register(body: UserRegisterRequest, background_tasks: BackgroundTasks):
    """Create a new user account and seed default folders."""
    # Check duplicate email
    existing_user = await User.find_one({"email": body.email.lower()})
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "An account with this email already exists."}
        )

    # Hash password and create user
    hashed = hash_password(body.password)
    user = User(
        email=body.email.lower(),
        hashed_password=hashed,
        name=body.name.strip(),
        google_id=None,
        avatar_url=f"https://api.dicebear.com/7.x/initials/svg?seed={body.name.strip()}",
    )
    await user.insert()

    # Seed default file structure for the user
    await seed_user_data(str(user.id))

    # Sync seeded data with Backblaze B2 in the background
    from app.b2 import check_and_sync_user
    background_tasks.add_task(check_and_sync_user, str(user.id))

    # Generate JWT token
    token = create_access_token(data={"sub": str(user.id)})

    return AuthTokenResponse(
        token=token,
        user=_to_user_response(user)
    )


@router.post(
    "/login",
    response_model=AuthTokenResponse,
    summary="Log in with email and password",
    responses={401: {"model": ErrorResponse}},
)
async def login(body: UserLoginRequest, background_tasks: BackgroundTasks):
    """Authenticate email and password and return a JWT access token."""
    user = await User.find_one({"email": body.email.lower()})
    if not user or not user.hashed_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Invalid email or password."}
        )

    if not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Invalid email or password."}
        )

    token = create_access_token(data={"sub": str(user.id)})

    # Retroactively check and sync files with B2 in the background
    from app.b2 import check_and_sync_user
    background_tasks.add_task(check_and_sync_user, str(user.id))

    return AuthTokenResponse(
        token=token,
        user=_to_user_response(user)
    )


@router.post(
    "/google",
    response_model=AuthTokenResponse,
    summary="Authentication via Google Sign-In",
    responses={400: {"model": ErrorResponse}},
)
async def login_with_google(body: GoogleLoginRequest, background_tasks: BackgroundTasks):
    """Receive Google ID token, verify it, create/find user, and return JWT."""
    google_profile = await verify_google_token(body.credential)

    google_id = google_profile.get("sub")
    email = google_profile.get("email", "").lower()
    name = google_profile.get("name", "Google User")
    picture = google_profile.get("picture")

    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Google account does not provide email address."}
        )

    # Check if user already exists by google_id or email
    user = await User.find_one({"google_id": google_id})
    if not user:
        user = await User.find_one({"email": email})

    is_new = False
    if not user:
        # Create a new Google user
        user = User(
            email=email,
            hashed_password=None,
            name=name,
            google_id=google_id,
            avatar_url=picture,
        )
        await user.insert()
        is_new = True
    else:
        # Link Google ID or update avatar if not set
        updates = {}
        if not user.google_id:
            updates["google_id"] = google_id
        if picture and (not user.avatar_url or "dicebear.com" in user.avatar_url):
            updates["avatar_url"] = picture

        if updates:
            await user.update({"$set": updates})
            user = await User.get(user.id)

    # Seed files if user is brand new
    if is_new:
        await seed_user_data(str(user.id))

    # Generate JWT token
    token = create_access_token(data={"sub": str(user.id)})

    # Retroactively check and sync files with B2 in the background
    from app.b2 import check_and_sync_user
    background_tasks.add_task(check_and_sync_user, str(user.id))

    return AuthTokenResponse(
        token=token,
        user=_to_user_response(user)
    )


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get profile of current logged in user",
    responses={401: {"model": ErrorResponse}},
)
async def get_me(current_user: User = Depends(get_current_user)):
    """Retrieve profile of the currently authenticated user."""
    return _to_user_response(current_user)


@router.put(
    "/profile",
    response_model=AuthTokenResponse,
    summary="Update profile of current logged in user",
    responses={401: {"model": ErrorResponse}},
)
async def update_profile(
    name: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    avatar: Optional[UploadFile] = File(None),
    current_user: User = Depends(get_current_user),
):
    """Update user profile. If avatar is provided, uploads to B2."""
    updates = {}
    if name is not None:
        updates["name"] = name
    if password is not None:
        updates["hashed_password"] = hash_password(password)

    if avatar:
        # Upload directly to B2 avatar path
        from app.b2 import get_b2_client
        from app.config import get_settings
        import uuid
        import mimetypes

        settings = get_settings()
        b2 = get_b2_client()
        if not b2:
            raise HTTPException(status_code=500, detail="B2 storage not configured")

        ext = mimetypes.guess_extension(avatar.content_type) or ".jpg"
        # Create a unique filename for the avatar
        filename = f"avatars/{current_user.id}/{uuid.uuid4().hex}{ext}"

        file_bytes = await avatar.read()
        try:
            b2.put_object(
                Bucket=settings.B2_BUCKET,
                Key=filename,
                Body=file_bytes,
                ContentType=avatar.content_type,
            )
            # Construct raw B2 URL
            b2_url = f"https://f005.backblazeb2.com/file/{settings.B2_BUCKET}/{filename}"
            updates["avatar_url"] = b2_url
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to upload avatar to B2: {str(e)}")

    if updates:
        await current_user.update({"$set": updates})
        # Refresh current_user from DB
        current_user = await User.get(current_user.id)

    # Return a new token and user response (since frontend expects {token, user})
    token = create_access_token(data={"sub": str(current_user.id)})
    return AuthTokenResponse(token=token, user=_to_user_response(current_user))
