"""File hashing utility for duplicate detection."""

import hashlib


def compute_file_sha256(filepath, chunk_size=8192):
    """
    Compute SHA-256 hash of a file, reading in chunks to handle large files.

    Args:
        filepath: Path to the file to hash
        chunk_size: Size of chunks to read at a time (default 8KB)

    Returns:
        64-character hex digest string
    """
    sha256 = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()
