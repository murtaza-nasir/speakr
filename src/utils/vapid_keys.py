"""
VAPID Key Management
Auto-generates and stores VAPID keys for push notifications
"""
import base64
import json
import os
from pathlib import Path


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def generate_vapid_keys():
    """Generate a new VAPID key pair with py_vapid.

    The public key is the base64url form of the 65-byte uncompressed P-256
    point, which browsers take as applicationServerKey. The private key is the
    base64url form of the 32-byte private scalar, the format that
    pywebpush.webpush(vapid_private_key=...) and `web-push generate-vapid-keys`
    use. Returns None when the key could not be generated.
    """
    try:
        from cryptography.hazmat.primitives import serialization
        from py_vapid import Vapid
    except ImportError as e:
        print(f"[VAPID] py_vapid is not installed ({e}). Push notifications will be disabled.")
        print("[VAPID] Install with: pip install pywebpush")
        return None

    try:
        vapid = Vapid()
        vapid.generate_keys()
        public_bytes = vapid.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        private_bytes = vapid.private_key.private_numbers().private_value.to_bytes(32, 'big')
        return {
            'public_key': _b64url(public_bytes),
            'private_key': _b64url(private_bytes),
        }
    except Exception as e:
        print(f"[VAPID] Failed to generate keys: {e!r}")
        return None


def get_vapid_keys_file():
    """The file of the VAPID key pair.

    CONFIG_DIR when it is set; otherwise /data/instance, the directory of the
    secret key, which the example compose file keeps on a volume. A key pair
    that is lost with a recreated container would end every push
    subscription at each update.
    """
    config_dir = Path(os.getenv('CONFIG_DIR') or '/data/instance')
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / 'vapid_keys.json'


def _valid_keys(keys):
    return (
        isinstance(keys, dict)
        and isinstance(keys.get('public_key'), str) and keys['public_key']
        and isinstance(keys.get('private_key'), str) and keys['private_key']
    )


def load_vapid_keys():
    """Load existing VAPID keys or generate new ones"""
    try:
        keys_file = get_vapid_keys_file()
    except OSError as e:
        print(f"[VAPID] Cannot use the key directory: {e}. Push notifications will be disabled.")
        return None

    # Try to load existing keys
    if keys_file.exists():
        try:
            with open(keys_file, 'r') as f:
                keys = json.load(f)
            if _valid_keys(keys):
                print(f"[VAPID] Loaded existing keys from {keys_file}")
                return {'public_key': keys['public_key'], 'private_key': keys['private_key']}
            print(f"[VAPID] {keys_file} has no usable key pair; generating a new one")
        except Exception as e:
            print(f"[VAPID] Failed to load existing keys: {e}")
            # Continue to generate new keys

    # Generate new keys
    print("[VAPID] Generating new VAPID keys...")
    keys = generate_vapid_keys()
    if not keys:
        print("[VAPID] Push notifications disabled - no VAPID key pair available")
        return None

    # Save keys to file, readable and writable by the owner only from the start
    try:
        tmp_file = keys_file.with_name(keys_file.name + '.tmp')
        fd = os.open(tmp_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(keys, f, indent=2)
        os.chmod(tmp_file, 0o600)
        os.replace(tmp_file, keys_file)
        print(f"[VAPID] Saved new keys to {keys_file}")
        print(f"[VAPID] Public key: {keys['public_key'][:50]}...")
    except Exception as e:
        print(f"[VAPID] Failed to save keys: {e}")
    return keys


# Initialize on module import
VAPID_KEYS = load_vapid_keys()
VAPID_ENABLED = VAPID_KEYS is not None

# Make keys available as module-level variables
VAPID_PUBLIC_KEY = VAPID_KEYS['public_key'] if VAPID_KEYS else None
VAPID_PRIVATE_KEY = VAPID_KEYS['private_key'] if VAPID_KEYS else None
