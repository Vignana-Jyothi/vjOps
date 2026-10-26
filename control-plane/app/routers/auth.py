from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import audit, current_user, require_roles
from ..models import Role, User
from ..schemas import PasswordChange, SignupRequest, Token, UserCreate, UserOut, UserUpdate
from ..security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=Token)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == form.username.lower().strip()).first()
    if not user or not verify_password(form.password, user.hashed_password) or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    audit(db, "auth.login", actor=user.email, actor_role=user.role)
    return Token(
        access_token=create_access_token(user.id, user.role),
        role=user.role,
        name=user.name,
        email=user.email,
    )


@router.post("/signup", response_model=Token, status_code=201)
def signup(payload: SignupRequest, db: Session = Depends(get_db)):
    # Self-service accounts are always students; elevated roles are granted by an admin via /users.
    email = payload.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with that email already exists")
    user = User(
        email=email,
        name=payload.name.strip() or email.split("@")[0],
        role=Role.student.value,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    audit(db, "auth.signup", actor=user.email, actor_role=user.role, target_type="user", target_id=user.id)
    return Token(
        access_token=create_access_token(user.id, user.role),
        role=user.role,
        name=user.name,
        email=user.email,
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user


@router.post("/change-password", status_code=204)
def change_password(payload: PasswordChange, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    user.hashed_password = hash_password(payload.new_password)
    db.commit()
    audit(db, "auth.change_password", actor=user.email, actor_role=user.role, target_type="user", target_id=user.id)


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles(Role.admin)),
):
    email = payload.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with that email already exists")
    user = User(
        email=email,
        name=payload.name or email.split("@")[0],
        role=payload.role,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    audit(db, "user.create", actor=actor.email, actor_role=actor.role, target_type="user", target_id=user.id, detail={"role": user.role})
    return user


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_roles(Role.admin, Role.devops))):
    return db.query(User).order_by(User.created_at.desc()).limit(500).all()


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: str,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles(Role.admin)),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    changes = payload.model_dump(exclude_none=True)
    # An admin locking themselves out leaves nobody able to undo it.
    if user.id == actor.id and (changes.get("role", Role.admin.value) != Role.admin.value or changes.get("is_active") is False):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot demote or deactivate your own account")
    if "password" in changes:
        user.hashed_password = hash_password(changes.pop("password"))
        changes["password_reset"] = True
    for k in ("name", "role", "is_active"):
        if k in changes:
            setattr(user, k, changes[k])
    db.commit()
    audit(db, "user.update", actor=actor.email, actor_role=actor.role, target_type="user", target_id=user.id, detail=changes)
    return user
