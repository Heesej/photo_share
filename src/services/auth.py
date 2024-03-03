from typing import Optional, Dict, Union, Callable

import redis as redis
from jose import JWTError, jwt
from fastapi import HTTPException, status, Depends
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
import pickle

from src.database.db import get_db
from src.database.models import User
from src.repository import users as repository_users

from src.services.secrets_manager import get_secret

REDIS_HOST = get_secret("REDIS_HOST")
REDIS_PORT = get_secret("REDIS_PORT")
REDIS_PASSWORD = get_secret("REDIS_PASSWORD")
SECRET_KEY = get_secret("SECRET_KEY")
ALGORITHM = get_secret("ALGORITHM")

class Auth:
    """
    Authentication service class.

    Attributes:
        pwd_context (CryptContext): Password hashing context.
        SECRET_KEY (str): Secret key for token encoding and decoding.
        ALGORITHM (str): Algorithm used for token encoding and decoding.
        oauth2_scheme (OAuth2PasswordBearer): OAuth2 password bearer for token retrieval.
        r (redis.Redis): Redis instance for caching user data.
    """
    pwd_context = CryptContext(schemes=["bcrypt"],
                               deprecated="auto")
    SECRET_KEY = SECRET_KEY
    ALGORITHM = ALGORITHM
    oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
    r = redis.Redis(host=REDIS_HOST,
                    port=REDIS_PORT,
                    password=REDIS_PASSWORD)

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """
        Verify the plain password against the hashed password.

        Args:
            plain_password (str): The plain text password.
            hashed_password (str): The hashed password.

        Returns:
            bool: True if passwords match, False otherwise.
        """
        return self.pwd_context.verify(plain_password, hashed_password)

    def get_password_hash(self, password: str) -> str:
        """
        Generate a hashed password.

        Args:
            password (str): The password to hash.

        Returns:
            str: The hashed password.
        """
        return self.pwd_context.hash(password)

    async def upgrade_password(self, user: User, password: str, db: Session) -> None:
        password_hash = self.get_password_hash(password)
        user.password = password_hash
        db.commit()

    def create_access_token(self, data: Dict[str, Union[str, int]], expires_delta: Optional[float] = None) -> str:
        """
        Create an access token.

        Args:
            data (Dict[str, Union[str, int]]): Payload data for the token.
            expires_delta (Optional[float]): Optional expiration time for the token.

        Returns:
            str: The encoded access token.
        """
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.utcnow() + timedelta(seconds=expires_delta)
        else:
            expire = datetime.utcnow() + timedelta(minutes=15)
        to_encode.update({"iat": datetime.utcnow(), "exp": expire, "scope": "access_token"})
        encoded_access_token = jwt.encode(to_encode, self.SECRET_KEY, algorithm=self.ALGORITHM)
        return encoded_access_token

    def create_refresh_token(self, data: Dict[str, Union[str, int]], expires_delta: Optional[float] = None) -> str:
        """
        Create a refresh token.

        Args:
            data (Dict[str, Union[str, int]]): Payload data for the token.
            expires_delta (Optional[float]): Optional expiration time for the token.

        Returns:
            str: The encoded refresh token.
        """
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.utcnow() + timedelta(seconds=expires_delta)
        else:
            expire = datetime.utcnow() + timedelta(days=7)
        to_encode.update({"iat": datetime.utcnow(), "exp": expire, "scope": "refresh_token"})
        encoded_refresh_token = jwt.encode(to_encode, self.SECRET_KEY, algorithm=self.ALGORITHM)
        return encoded_refresh_token

    async def decode_refresh_token(self, refresh_token: str) -> str:
        """
        Decode a refresh token.

        Args:
            refresh_token (str): The refresh token to decode.

        Returns:
            str: The decoded email from the refresh token.

        Raises:
            HTTPException: If decoding fails or the token scope is invalid.
        """
        try:
            payload = jwt.decode(refresh_token, self.SECRET_KEY, algorithms=[self.ALGORITHM])
            if payload['scope'] == 'refresh_token':
                email = payload['sub']
                return email
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid scope for token')
        except JWTError:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Could not validate credentials')

    async def get_current_user(self, token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> Dict:
        """
        Get the current user from the token.

        Args:
            token (str): The access token.
            db (Session): SQLAlchemy database session.

        Returns:
            Dict: The user data.

        Raises:
            HTTPException: If validation fails or user is not found.
        """
        credentials_exception = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

        try:
            payload = jwt.decode(token, self.SECRET_KEY, algorithms=[self.ALGORITHM])
            if payload['scope'] == 'access_token':
                email = payload["sub"]
                if email is None:
                    raise credentials_exception
            else:
                raise credentials_exception
        except JWTError as e:
            raise credentials_exception
        user = self.r.get(f"user:{email}")
        if user is None:
            user = await repository_users.get_user_by_email(email, db)
            if user is None:
                raise credentials_exception
            self.r.set(f"user:{email}", pickle.dumps(user))
            self.r.expire(f"user:{email}", 900)
        else:
            user = pickle.loads(user)
        return user


    def create_email_token(self, data: Dict[str, Union[str, int]]) -> str:
        """
        Create an email token.

        Args:
            data (Dict[str, Union[str, int]]): Payload data for the token.

        Returns:
            str: The encoded email token.
        """
        to_encode = data.copy()
        expire = datetime.utcnow() + timedelta(days=7)
        to_encode.update({"iat": datetime.utcnow(), "exp": expire})
        token = jwt.encode(to_encode, self.SECRET_KEY, algorithm=self.ALGORITHM)
        return token
    
    def admin_required(self, func: Callable):
        async def wrapper(current_user: User = Depends(self.get_current_user)):
            """
        Decorator to restrict access to admin users only.

        Parameters:
            func (Callable): The function to be decorated.

        Returns:
            Callable: Decorated function with access restriction to admin users.
            """
            if not current_user.admin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You are not an admin",
                )
            return await func(current_user=current_user)
        return wrapper
    
    def moderator_required(self, func: Callable):
        async def wrapper(current_user: User = Depends(self.get_current_user)):
            """
            Wrapper function to check if the current user is an admin.

            Parameters:
                current_user (User, optional): The current user. Defaults to Depends(self.get_current_user).

            Returns:
                Any: Result of the decorated function.
            """
            if not current_user.moderator:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You are not a moderator",
                )
            return await func(current_user=current_user)
        return wrapper

    async def get_email_from_token(self, token: str) -> str:
        """
        Get the email from an email token.

        Args:
            token (str): The email token.

        Returns:
            str: The decoded email from the token.

        Raises:
            HTTPException: If decoding fails or token is invalid.
        """
        try:
            payload = jwt.decode(token, self.SECRET_KEY, algorithms=[self.ALGORITHM])
            email = payload["sub"]
            return email
        except JWTError as e:
            print(e)
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="Invalid token for email.")
        


auth_service = Auth()