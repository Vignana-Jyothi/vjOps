from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import audit, current_user, require_roles
from ..models import Role, User
from ..schemas import Token, UserCreate, UserOut
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


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user


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
