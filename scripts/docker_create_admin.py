#!/usr/bin/env python3

import os
import sys
from email_validator import validate_email, EmailNotValidError

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Try to import from app context
try:
    from flask import current_app
    app = current_app._get_current_object()
    with app.app_context():
        db = app.extensions['sqlalchemy'].db
        User = app.extensions['sqlalchemy'].db.metadata.tables['user']
        bcrypt = app.extensions.get('bcrypt')
except (RuntimeError, AttributeError, KeyError):
    # If not in app context, import directly
    try:
        from app import app, db, User, bcrypt
    except ImportError as e:
        print(f"Error: Could not import required modules: {e}")
        print("Make sure docker_create_admin.py is runnable and PYTHONPATH is set.")
        sys.exit(1)

def create_admin_user_from_env():
    """
    Create an admin user from environment variables.
    Required environment variables:
    - ADMIN_USERNAME
    - ADMIN_EMAIL
    - ADMIN_PASSWORD
    """
    print("Creating admin user for Speakr application from environment variables")
    print("=================================================================")
    
    # Get values from environment variables
    username = os.environ.get('ADMIN_USERNAME')
    email = os.environ.get('ADMIN_EMAIL')
    password = os.environ.get('ADMIN_PASSWORD')
    
    # Validate required environment variables
    if not username or not email or not password:
        print("Error: ADMIN_USERNAME, ADMIN_EMAIL, and ADMIN_PASSWORD environment variables must be set.")
        sys.exit(1)
    
    # Validate username
    if len(username) < 3:
        print("Error: Username must be at least 3 characters long.")
        sys.exit(1)
    
    # Validate email
    try:
        validate_email(email)
    except EmailNotValidError as e:
        print(f"Error: Invalid email: {str(e)}")
        sys.exit(1)
    
    # Validate password
    if len(password) < 8:
        print("Error: Password must be at least 8 characters long.")
        sys.exit(1)
    
    # Create user
    with app.app_context():
        # Check if username already exists
        existing_user = db.session.query(User).filter_by(username=username).first()
        if existing_user:
            print(f"User with username '{username}' already exists.")
            sys.exit(0)
        
        # Check if email already exists
        existing_email = db.session.query(User).filter_by(email=email).first()
        if existing_email:
            print(f"User with email '{email}' already exists.")
            sys.exit(0)
        
        # Create new admin user
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        new_user = User(username=username, email=email, password=hashed_password, is_admin=True)
        db.session.add(new_user)
        db.session.commit()
        
        print("\nAdmin user created successfully!")
        print(f"Username: {username}")
        print(f"Email: {email}")
        print("You can now log in to the application with these credentials.")

if __name__ == "__main__":
    create_admin_user_from_env()
