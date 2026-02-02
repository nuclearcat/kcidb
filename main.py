"""Google Cloud Functions for Kernel CI reporting"""

import gc
import os
import time
import atexit
import tempfile
import base64
import json
import datetime
import logging
import smtplib
from urllib.parse import unquote
import jsonschema
import functions_framework
import google.cloud.logging
import kcidb

# Name of the Google Cloud project we're deployed in
PROJECT_ID = os.environ["GCP_PROJECT"]

# Setup Google Cloud logging, unless testing
if PROJECT_ID != "TEST_PROJECT":
    google.cloud.logging.Client().setup_logging()
    # Give logging some time to avoid occasional deployment failures
    time.sleep(5)
# Setup KCIDB-specific logging
kcidb.misc.logging_setup(
    kcidb.misc.LOGGING_LEVEL_MAP[os.environ.get("KCIDB_LOG_LEVEL", "NONE")]
)
# Get the module's logger
LOGGER = logging.getLogger()

# The specification for the operational database (a part of DATABASE spec)
OPERATIONAL_DATABASE = os.environ["KCIDB_OPERATIONAL_DATABASE"]

# The specification for the archive database (a part of DATABASE spec)
ARCHIVE_DATABASE = os.environ["KCIDB_ARCHIVE_DATABASE"]

# The specification for the archive sample database (a part of DATABASE spec)
SAMPLE_DATABASE = os.environ["KCIDB_SAMPLE_DATABASE"]

# The address of the SMTP host to send notifications through
SMTP_HOST = os.environ["KCIDB_SMTP_HOST"]
# The port of the SMTP server to send notifications through
SMTP_PORT = int(os.environ["KCIDB_SMTP_PORT"])
# The username to authenticate to SMTP server with
SMTP_USER = os.environ["KCIDB_SMTP_USER"]
# The SMTP user's password
_SMTP_PASSWORD = None
# The address to tell the SMTP server to send the message to,
# overriding any recipients in the message itself.
SMTP_TO_ADDRS = os.environ.get("KCIDB_SMTP_TO_ADDRS", None)
# The address to use as the "From" address in sent notifications
SMTP_FROM_ADDR = os.environ.get("KCIDB_SMTP_FROM_ADDR", None)
# A string to be added to the CC header of notifications being sent out
EXTRA_CC = os.environ.get("KCIDB_EXTRA_CC", None)
# The dictionary of database specs and client instances
_DB_CLIENTS = {}
# The dictionary of database specs and object-oriented client instances
_OO_CLIENTS = {}
# KCIDB cache client instance
_CACHE_CLIENT = None
# The notification spool client
_SPOOL_CLIENT = None
# KCIDB cache storage bucket name
CACHE_BUCKET_NAME = os.environ.get("KCIDB_CACHE_BUCKET_NAME")


def get_smtp_password():
    """Get the (cached) password for the SMTP user"""
    # It's alright, pylint: disable=global-statement
    global _SMTP_PASSWORD
    if _SMTP_PASSWORD is None:
        secret = os.environ["KCIDB_SMTP_PASSWORD_SECRET"]
        _SMTP_PASSWORD = kcidb.misc.get_secret(PROJECT_ID, secret)
    return _SMTP_PASSWORD


def get_db_credentials():
    """Fetch the database credentials, if needed and present"""
    if "PGPASSFILE" in os.environ:
        return
    secret_id = os.environ.get("KCIDB_PGPASS_SECRET")
    if secret_id is None:
        return

    pgpass = kcidb.misc.get_secret(PROJECT_ID, secret_id)
    (pgpass_fd, pgpass_filename) = tempfile.mkstemp(suffix=".pgpass")
    with os.fdopen(pgpass_fd, mode="w", encoding="utf-8") as pgpass_file:
        pgpass_file.write(pgpass)
    os.environ["PGPASSFILE"] = pgpass_filename
    atexit.register(os.remove, pgpass_filename)


def get_db_client(database):
    """
    Create or retrieve the cached database client.

    Args:
        database:   The specification for the database the client should
                    connect to.
    """
    if database not in _DB_CLIENTS:
        # Get the credentials
        get_db_credentials()
        # Create the client
        _DB_CLIENTS[database] = kcidb.db.Client(database)
    return _DB_CLIENTS[database]


def get_oo_client(database):
    """
    Create or retrieve the cached OO database client.

    Args:
        database:   The specification for the database the client should
                    connect to.
    """
    if database not in _OO_CLIENTS:
        _OO_CLIENTS[database] = kcidb.oo.Client(get_db_client(database))
    return _OO_CLIENTS[database]


def get_spool_client():
    """Create or retrieve the cached notification spool client."""
    # It's alright, pylint: disable=global-statement
    global _SPOOL_CLIENT
    if _SPOOL_CLIENT is None:
        collection_path = os.environ["KCIDB_SPOOL_COLLECTION_PATH"]
        _SPOOL_CLIENT = kcidb.monitor.spool.Client(collection_path)
    return _SPOOL_CLIENT


def kcidb_send_notification(data, context):
    """
    Send notifications from the spool
    """
    spool_client = get_spool_client()
    # Get the notification ID
    notification_id = context.resource.split("/")[-1]
    # Pick the notification if we can
    message = spool_client.pick(notification_id)
    if not message:
        return
    LOGGER.info("SENDING %s", notification_id)
    # send message via email
    send_message(message)
    # Acknowledge notification as sent
    spool_client.ack(notification_id)


def kcidb_pick_notifications(data, context):
    """
    Pick abandoned notifications and send them.
    """
    spool_client = get_spool_client()
    for notification_id in spool_client.unpicked():
        # Pick abandoned notification and resend
        message = spool_client.pick(notification_id)
        if not message:
            continue
        LOGGER.info("SENDING %s", notification_id)
        # send message via email
        send_message(message)
        # Acknowledge notification as sent
        spool_client.ack(notification_id)


def kcidb_archive(event, context):
    """
    Transfer data from the operational database into the archive database,
    that is out of the editing window (to be enforced), and hasn't been
    transferred yet.
    """
    # It's OK, pylint: disable=too-many-locals
    #
    # Describe the expected event data
    params_schema = dict(
        type="object",
        properties=dict(
            data_min_age=dict(
                type="integer", minimum=0,
                description="Minimum age of data, seconds"
            ),
            data_max_duration=dict(
                type="integer", minimum=0,
                description="Maximum data duration, seconds. "
                            "No limit, if missing."
            ),
            data_chunk_duration=dict(
                type="integer", minimum=0,
                description="Data chunk duration, seconds"
            ),
            run_max_duration=dict(
                type="integer", minimum=0,
                description="Maximum runtime, seconds"
            ),
        ),
        required=[
           "data_min_age",
           "data_chunk_duration",
           "run_max_duration",
        ],
        additionalProperties=False,
    )

    # Parse the input JSON
    params_string = base64.b64decode(event["data"]).decode()
    params = json.loads(params_string)
    jsonschema.validate(
        instance=params, schema=params_schema,
        format_checker=jsonschema.Draft7Validator.FORMAT_CHECKER
    )
    LOGGER.info("Archiving parameters: %s", json.dumps(params))

    # Minimum data age (editing window, to be enforced)
    data_min_age = datetime.timedelta(
        seconds=int(params["data_min_age"])
    )
    # Maximum duration of the data transferred in a single execution
    # Operational database cannot have gaps of this or greater duration
    data_max_duration = (
        datetime.timedelta(seconds=int(params["data_max_duration"]))
        if "data_max_duration" in params else
        None
    )
    # Duration of each data chunk
    data_chunk_duration = datetime.timedelta(
        seconds=int(params["data_chunk_duration"])
    )

    # Execution (monotonic) deadline
    deadline_monotonic = time.monotonic() + int(params["run_max_duration"])

    op_client = get_db_client(OPERATIONAL_DATABASE)
    op_io_schema = op_client.get_schema()[1]
    op_obj_list_names = set(op_io_schema.id_fields)
    op_now = op_client.get_current_time()
    op_first_modified = op_client.get_first_modified()
    if not op_first_modified:
        LOGGER.info("Operational database is empty, nothing to archive, "
                    "aborting")
        return

    ar_client = get_db_client(ARCHIVE_DATABASE)
    ar_last_modified = ar_client.get_last_modified()

    # Find the timestamps right before the data we need to fetch
    after = {
        n: (
            ar_last_modified.get(n) or
            op_first_modified.get(n) and
            op_first_modified[n] - datetime.timedelta(seconds=1)
        ) for n in op_obj_list_names
    }
    min_after = min(after.values())

    # Find the maximum timestamp of the data we need to fetch
    # We try to align all tables on a single time boundary
    until = min(
        datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
        if data_max_duration is None else
        min_after + data_max_duration,
        op_now - data_min_age
    )

    if min_after >= until:
        LOGGER.info("No data old enough to archive, aborting")
        return

    # Transfer data in pieces which can hopefully fit in memory
    # Split by time, down to microseconds, as it's our transfer atom
    min_after_str = min_after.isoformat(timespec='microseconds')
    first_min_after_str = min_after_str
    total_count = 0
    while all(t < until for t in after.values()):
        if time.monotonic() >= deadline_monotonic:
            LOGGER.info("Ran out of time, stopping")
            break
        next_after = {
            n: min(max(t, min_after + data_chunk_duration), until)
            for n, t in after.items()
        }
        next_min_after = min(next_after.values())
        next_min_after_str = next_min_after.isoformat(timespec='microseconds')
        # Transfer the data, preserving the timestamps
        LOGGER.info("FETCHING operational database data for (%s, %s] range",
                    min_after_str, next_min_after_str)
        for obj_list_name in after:
            LOGGER.debug(
                "FETCHING %s for (%s, %s] range",
                obj_list_name,
                after[obj_list_name].isoformat(timespec='microseconds'),
                next_after[obj_list_name].isoformat(timespec='microseconds')
            )
        data = op_client.dump(with_metadata=True,
                              after=after, until=next_after)
        count = kcidb.io.SCHEMA.count(data)
        LOGGER.info("LOADING %u objects into archive database", count)
        ar_client.load(data, with_metadata=True, copy=False)
        LOGGER.info("ARCHIVED %u objects in (%s, %s] range",
                    count, min_after_str, next_min_after_str)
        for obj_list_name in after:
            LOGGER.debug("ARCHIVED %u %s",
                         len(data.get(obj_list_name, [])), obj_list_name)
        total_count += count
        after = next_after
        min_after = next_min_after
        min_after_str = next_min_after_str
        # Make sure we have enough memory for the next piece
        data = None
        gc.collect()
    else:
        LOGGER.info("Completed, stopping")

    LOGGER.info("ARCHIVED %u objects TOTAL in (%s, %s] range",
                total_count, first_min_after_str, min_after_str)


def kcidb_purge_db(event, context):
    """
    Purge data from the operational database, older than the optional delta
    from the current (or specified) database timestamp, rounded to smallest
    delta component. Require that either the delta or the timestamp are
    present.
    """
    # Accepted databases and their specs
    databases = dict(op=OPERATIONAL_DATABASE, sm=SAMPLE_DATABASE)

    # Describe the expected event data
    schema = dict(
        type="object",
        properties=dict(
            database=dict(type="string", enum=list(databases)),
            timedelta=kcidb.misc.TIMEDELTA_JSON_SCHEMA,
        )
    )

    # Parse the input JSON
    string = base64.b64decode(event["data"]).decode()
    data = json.loads(string)
    jsonschema.validate(
        instance=data, schema=schema,
        format_checker=jsonschema.Draft7Validator.FORMAT_CHECKER
    )

    # Get the database client
    client = get_db_client(databases[data["database"]])

    # Parse/calculate the cut-off timestamp
    stamp = kcidb.misc.timedelta_json_parse(data["timedelta"],
                                            client.get_current_time())

    # Purge the data
    client.purge(stamp)


def send_message(message):
    """
    Send message via email.

    Args:
        message:    The message to send.
    """
    # Set From address, if specified
    if SMTP_FROM_ADDR:
        message['From'] = SMTP_FROM_ADDR
    # Add extra CC, if specified
    if EXTRA_CC:
        cc_addrs = message["CC"]
        if cc_addrs:
            message.replace_header("CC", cc_addrs + ", " + EXTRA_CC)
        else:
            message["CC"] = EXTRA_CC
    # Connect to the SMTP server
    smtp = smtplib.SMTP(host=SMTP_HOST, port=SMTP_PORT)
    smtp.ehlo()
    smtp.starttls()
    smtp.ehlo()
    smtp.login(SMTP_USER, get_smtp_password())
    try:
        # Send message
        smtp.send_message(message, to_addrs=SMTP_TO_ADDRS)
    finally:
        # Disconnect from the SMTP server
        smtp.quit()


def get_cache_client():
    """Create the cache client."""
    # It's alright, pylint: disable=global-statement
    global _CACHE_CLIENT
    if _CACHE_CLIENT is None:
        chunk_size = 1024 * 1024
        max_size = 5 * chunk_size
        _CACHE_CLIENT = kcidb.cache.Client(
            CACHE_BUCKET_NAME, max_size, chunk_size
        )
    return _CACHE_CLIENT


# The expiration time (a timedelta) of the URLs returned by the cache
# redirect, or None to return permanent URLs pointing to the public bucket.
CACHE_REDIRECT_TTL = datetime.timedelta(seconds=10)


@functions_framework.http
def kcidb_cache_redirect(request):
    """
    Handle the cache redirection for incoming HTTP GET requests.

    This function takes an HTTP request and processes it for
    cache redirection. If the request is a GET request,
    it extracts the URL from the request, checks if the URL exists
    in the cache, and performs a redirect if necessary.

    Args:
        request (object): The HTTP request object.

    Returns:
        tuple: A tuple containing the response body, status code,
        and headers epresenting the redirect response.
    """
    if request.method == 'GET':
        url_to_fetch = unquote(request.query_string.decode("ascii"))
        LOGGER.debug("URL %r", url_to_fetch)

        if not url_to_fetch:
            # If the URL is empty, return a 400 (Bad Request) error
            response_body = "Provide a valid URL to query from " \
                "the caching system."
            return (response_body, 400, {})

        # Check if the URL is in the cache
        cache_client = get_cache_client()
        cache = cache_client.map(url_to_fetch, ttl=CACHE_REDIRECT_TTL)
        if cache:
            LOGGER.info("Redirecting to the cache at %r", cache)
            # Redirect to the cached URL if it exists
            return ("", 302, {"Location": cache})

        # If the URL is not in the cache or not provided,
        # redirect to the original URL
        LOGGER.info("Redirecting to the origin at %r", url_to_fetch)
        return ("", 302, {"Location": url_to_fetch})

    # If the request method is not GET, return 405 (Method Not Allowed) error
    response_body = "Method not allowed."
    return (response_body, 405, {'Allow': 'GET'})
