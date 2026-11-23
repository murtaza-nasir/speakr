"""Size of the database connection pool of one process (#667).

Every request thread and every job thread holds a connection while it
works. The process that owns the job queue runs the request threads of its
gunicorn worker (8 in the image) and also the job threads (transcription,
summary, index and convert workers), the webhook dispatcher and the
watch-folder monitor. SQLAlchemy's default of 5 + 10 connections left
requests waiting for the 30 s pool timeout under load.

The defaults below cover those threads with a margin. DB_POOL_SIZE,
DB_MAX_OVERFLOW and DB_POOL_TIMEOUT override them; with PostgreSQL the sum
over all gunicorn workers must stay below the server's max_connections.
"""

import logging

logger = logging.getLogger(__name__)

# gthread threads per gunicorn worker in the image (Dockerfile CMD).
REQUEST_THREADS = 8
# Webhook dispatcher and watch-folder monitor.
SERVICE_THREADS = 2
MARGIN = 4
DEFAULT_POOL_SIZE = 10
DEFAULT_POOL_TIMEOUT = 30

_WORKER_ENV = (('JOB_QUEUE_WORKERS', 2), ('SUMMARY_QUEUE_WORKERS', 2),
               ('INDEX_QUEUE_WORKERS', 1), ('CONVERT_QUEUE_WORKERS', 2))


def _int(environ, name, default, minimum=0):
    raw = environ.get(name)
    if raw is None or str(raw).strip() == '':
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a whole number; using {default}")
        return default
    if value < minimum:
        logger.warning(f"{name}={value} is below {minimum}; using {default}")
        return default
    return value


def job_threads(environ):
    """Job worker threads of the process that owns the queue."""
    return sum(_int(environ, name, default) for name, default in _WORKER_ENV)


def pool_options(environ, database_uri):
    """pool_size, max_overflow and pool_timeout for the engine; {} for an in-memory SQLite.

    An in-memory SQLite database uses a single-connection pool that takes
    none of these arguments.
    """
    uri = database_uri or ''
    if uri.startswith('sqlite') and (':memory:' in uri or uri.rstrip('/') in ('sqlite:', 'sqlite')):
        return {}
    pool_size = _int(environ, 'DB_POOL_SIZE', DEFAULT_POOL_SIZE, minimum=1)
    needed = REQUEST_THREADS + job_threads(environ) + SERVICE_THREADS + MARGIN
    max_overflow = _int(environ, 'DB_MAX_OVERFLOW', max(10, needed - pool_size))
    pool_timeout = _int(environ, 'DB_POOL_TIMEOUT', DEFAULT_POOL_TIMEOUT, minimum=1)
    return {'pool_size': pool_size, 'max_overflow': max_overflow, 'pool_timeout': pool_timeout}
