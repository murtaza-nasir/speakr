"""
VAPID Key Management
Auto-generates and stores VAPID keys for push notifications
"""
import os
import json
from pathlib import Path


def generate_vapid_keys():
    """Generate new VAPID keys using pywebpush"""
    try:
        from pywebpush import webpush

        # Generate keys
        vapid_claims = webpush.WebPusher().vapid_claims

        # For newer versions of pywebpush, use this approach:
        from py_vapid import Vapid
        vapid = Vapid()
        vapid.generate_keys()

        return {
            'public_key': vapid.public_key.export_public(encoding='uncompressed'),
            'private_key': vapid.private_key.export_private(encoding='pem')
        }
    except ImportError:
        print("[VAPID] pywebpush not installed. Push notifications will be disabled.")
        print("[VAPID] Install with: pip install pywebpush")
        return None
    except Exception as e:
        print(f"[VAPID] Failed to generate keys: {e}")
        return None


def get_vapid_keys_file():
    """Get path to VAPID keys storage file"""
    # Store in /config directory (persistent in Docker)
    config_dir = Path(os.getenv('CONFIG_DIR', '/config'))
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / 'vapid_keys.json'


def load_vapid_keys():
    """Load existing VAPID keys or generate new ones"""
    keys_file = get_vapid_keys_file()

    # Try to load existing keys
    if keys_file.exists():
        try:
            with open(keys_file, 'r') as f:
                keys = json.load(f)
                print(f"[VAPID] Loaded existing keys from {keys_file}")
                return keys
        except Exception as e:
            print(f"[VAPID] Failed to load existing keys: {e}")
            # Continue to generate new keys

    # Generate new keys
    print("[VAPID] Generating new VAPID keys...")
    keys = generate_vapid_keys()

    if keys:
        # Save keys to file
        try:
            with open(keys_file, 'w') as f:
                json.dump(keys, f, indent=2)

            # Set restrictive permissions (owner read/write only)
            os.chmod(keys_file, 0o600)

            print(f"[VAPID] Saved new keys to {keys_file}")
            print(f"[VAPID] Public key: {keys['public_key'][:50]}...")
            return keys
        except Exception as e:
            print(f"[VAPID] Failed to save keys: {e}")
            return keys
    else:
        print("[VAPID] Push notifications disabled - pywebpush not available")
        return None


def get_public_key():
    """Get the public VAPID key for client use"""
    keys = load_vapid_keys()
    return keys['public_key'] if keys else None


def get_private_key():
    """Get the private VAPID key for server use"""
    keys = load_vapid_keys()
    return keys['private_key'] if keys else None


# Initialize on module import
VAPID_KEYS = load_vapid_keys()
VAPID_ENABLED = VAPID_KEYS is not None

# Make keys available as module-level variables
VAPID_PUBLIC_KEY = VAPID_KEYS['public_key'] if VAPID_KEYS else None
VAPID_PRIVATE_KEY = VAPID_KEYS['private_key'] if VAPID_KEYS else None
