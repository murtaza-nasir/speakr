#!/usr/bin/env python3

import os
import sys
import getpass
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
        print("Make sure create_admin.py is runnable and PYTHONPATH is set.")
        sys.exit(1)

def create_admin_user():
    """
    Create an admin user interactively.
    """
    print("Creating admin user for Speakr application")
    print("=========================================")
    
    # Get username
    while True:
        username = input("Enter username (min 3 characters): ").strip()
        if len(username) < 3:
            print("Username must be at least 3 characters long.")
            continue
        
        # Check if username already exists
        with app.app_context():
            existing_user = db.session.query(User).filter_by(username=username).first()
            if existing_user:
                print(f"Username '{username}' already exists. Please choose another.")
                continue
            break
    
    # Get email
    while True:
        email = input("Enter email address: ").strip()
        try:
            # Validate email
            validate_email(email)
            
            # Check if email already exists
            with app.app_context():
                existing_email = db.session.query(User).filter_by(email=email).first()
                if existing_email:
                    print(f"Email '{email}' already exists. Please use another.")
                    continue
                break
        except EmailNotValidError as e:
            print(f"Invalid email: {str(e)}")
    
    # Get password
    while True:
        password = getpass.getpass("Enter password (min 8 characters): ")
        if len(password) < 8:
            print("Password must be at least 8 characters long.")
            continue
        
        confirm_password = getpass.getpass("Confirm password: ")
        if password != confirm_password:
            print("Passwords do not match. Please try again.")
            continue
        break
    
    # Create user
    with app.app_context():
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        new_user = User(username=username, email=email, password=hashed_password, is_admin=True)
        db.session.add(new_user)
        db.session.commit()
        
        print("\nAdmin user created successfully!")
        print(f"Username: {username}")
        print(f"Email: {email}")
        print("You can now log in to the application with these credentials.")

if __name__ == "__main__":
    create_admin_user()
