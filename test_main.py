"""main.py tests"""

import base64
import json
import os
import subprocess
import unittest
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from importlib import import_module
import yaml
import kcidb


@unittest.skipIf(os.environ.get("KCIDB_DEPLOYMENT"), "local-only")
def test_google_credentials_are_not_specified():
    """Check Google Application credentials are not specified"""
    assert os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") is None, \
        "Local tests must run without " \
        "GOOGLE_APPLICATION_CREDENTIALS " \
        "environment variable"


def test_import():
    """Check main.py can be loaded"""
    # Load deployment environment variables
    file_dir = os.path.dirname(os.path.abspath(__file__))
    cloud_path = os.path.join(file_dir, "cloud")
    env = yaml.safe_load(
        subprocess.check_output([
            cloud_path,
            "env", "kernelci-production", "",
            "--log-level=DEBUG"
        ])
    )
    env["GCP_PROJECT"] = "TEST_PROJECT"

    orig_env = dict(os.environ)
    try:
        os.environ.update(env)
        import_module("main")
    finally:
        os.environ.clear()
        os.environ.update(orig_env)


def test_purge_db(empty_deployment):
    """Check kcidb_purge_db() works correctly"""
    # It's OK, pylint: disable=too-many-locals

    # Make empty_deployment appear used to silence pylint warning
    assert empty_deployment is None

    # Each type of database, purging expectation, and client
    clients = dict(
        op=(True, kcidb.db.Client(os.environ["KCIDB_OPERATIONAL_DATABASE"])),
        sm=(True, kcidb.db.Client(os.environ["KCIDB_SAMPLE_DATABASE"])),
        ar=(False, kcidb.db.Client(os.environ["KCIDB_ARCHIVE_DATABASE"])),
    )

    # Determine the minimum supported I/O version
    min_io_version = min(c.get_schema()[1] for _, c in clients.values())

    # Use the current time to avoid deployment purge trigger
    timestamp_before = datetime.now(timezone.utc)
    str_before = timestamp_before.isoformat(timespec="microseconds")
    timestamp_cutoff = timestamp_before + timedelta(seconds=1)
    str_cutoff = timestamp_cutoff.isoformat(timespec="microseconds")

    data_before = dict(
        version=dict(
            major=min_io_version.major, minor=min_io_version.minor
        ),
        checkouts=[dict(
            id="origin:1", origin="origin",
            _timestamp=str_before
        )],
        builds=[dict(
            id="origin:1", origin="origin", checkout_id="origin:1",
            _timestamp=str_before
        )],
        tests=[dict(
            id="origin:1", origin="origin", build_id="origin:1",
            _timestamp=str_before
        )],
        issues=[dict(
            id="origin:1", origin="origin", version=1,
            _timestamp=str_before
        )],
        incidents=[dict(
            id="origin:1", origin="origin",
            issue_id="origin:1", issue_version=1,
            _timestamp=str_before
        )],
    )

    timestamp_after = timestamp_cutoff + timedelta(seconds=1)
    str_after = timestamp_after.isoformat(timespec="microseconds")

    data_after = dict(
        version=dict(
            major=min_io_version.major, minor=min_io_version.minor
        ),
        checkouts=[dict(
            id="origin:2", origin="origin",
            _timestamp=str_after
        )],
        builds=[dict(
            id="origin:2", origin="origin", checkout_id="origin:2",
            _timestamp=str_after
        )],
        tests=[dict(
            id="origin:2", origin="origin", build_id="origin:2",
            _timestamp=str_after
        )],
        issues=[dict(
            id="origin:2", origin="origin", version=1,
            _timestamp=str_after
        )],
        incidents=[dict(
            id="origin:2", origin="origin",
            issue_id="origin:2", issue_version=1,
            _timestamp=str_after
        )],
    )

    def filter_test_data(data):
        """Filter objects created by this test from I/O data"""
        return {
            key: [
                deepcopy(obj) for obj in value
                if obj.get("_timestamp") in (str_before, str_after)
            ] if key and key in min_io_version.graph
            else deepcopy(value)
            for key, value in data.items()
        }

    # Merge the before and after data
    data = min_io_version.merge(data_before, [data_after])

    # For each type of database, purging expectation, and client
    main = import_module("main")
    for database, (purging, client) in clients.items():
        client.load(data, with_metadata=True)
        dump = filter_test_data(client.dump())
        for obj_list_name in min_io_version.graph:
            if obj_list_name:
                assert len(dump.get(obj_list_name, [])) == 2, \
                    f"Invalid number of {obj_list_name} in " \
                    f"{database} database"

        # Trigger the purge at the boundary
        event = {
            "data": base64.b64encode(json.dumps({
                "database": database,
                "timedelta": {"stamp": str_cutoff}
            }).encode()).decode()
        }
        main.kcidb_purge_db(event, None)

        dump = filter_test_data(client.dump())
        assert dump == client.get_schema()[1].upgrade(
            data_after if purging else data
        ), "Unexpected data in {database} database"


def test_archive(empty_deployment):
    """Check kcidb_archive() works correctly"""
    # Make empty_deployment appear used to silence pylint warning
    assert empty_deployment is None

    op_client = kcidb.db.Client(os.environ["KCIDB_OPERATIONAL_DATABASE"])
    op_schema = op_client.get_schema()[1]
    ar_client = kcidb.db.Client(os.environ["KCIDB_ARCHIVE_DATABASE"])
    ar_schema = ar_client.get_schema()[1]
    main = import_module("main")

    # Empty the archive
    ar_client.empty()

    # Generate timestamps
    ts_now = op_client.get_current_time()
    ts_3w = ts_now - timedelta(days=7 * 3)
    ts_4w = ts_now - timedelta(days=7 * 4)

    def gen_data(id, ts):
        """
        Generate a dataset with one object per type, all using the specified
        timestamp, ID, and origin extracted from the ID.
        """
        assert isinstance(id, str)
        assert isinstance(ts, datetime) and ts.tzinfo
        origin = id.split(":")[0]
        assert origin
        assert origin != id
        base = dict(id=id, origin=origin,
                    _timestamp=ts.isoformat(timespec='microseconds'))
        return dict(
            checkouts=[base | dict()],
            builds=[base | dict(checkout_id=id)],
            tests=[base | dict(build_id=id)],
            issues=[base | dict(version=1)],
            incidents=[base | dict(issue_id=id, issue_version=1)],
            **op_schema.new(),
        )

    # Generate datasets
    data_now = gen_data("archive:now", ts_now)
    data_3w = gen_data("archive:3w", ts_3w)
    data_4w = gen_data("archive:4w", ts_4w)

    # Archival parameters
    params = dict(
        # Edit window: two weeks
        data_min_age=2 * 7 * 24 * 60 * 60,
        # Transfer at most one week
        data_max_duration=7 * 24 * 60 * 60,
        # Transfer one week at a time (everything in one go)
        data_chunk_duration=7 * 24 * 60 * 60,
        # We gotta be at least faster than the time we wait (60s)
        run_max_duration=45,
    )

    # Load data_now into the operational DB
    op_client.load(data_now, with_metadata=True)
    # Trigger archival
    event = {
        "data": base64.b64encode(json.dumps(params).encode()).decode()
    }
    main.kcidb_archive(event, None)
    # Check data_now doesn't end up in the archive DB
    assert ar_schema.count(ar_client.dump()) == 0

    # Load data_3w and data_4w
    op_client.load(op_schema.merge(data_3w, [data_4w]), with_metadata=True)
    # Trigger archival
    event = {
        "data": base64.b64encode(json.dumps(params).encode()).decode()
    }
    main.kcidb_archive(event, None)
    # Check data_4w is in the archive database
    dump = ar_client.dump()
    assert all(
        any(obj["id"] == "archive:4w"
            for obj in dump.get(obj_list_name, []))
        for obj_list_name in op_schema.id_fields
    ), "No complete four-week old data in the archive"
    # Check data_3w is not in the archive database
    assert not any(
        any(obj["id"] == "archive:3w"
            for obj in dump.get(obj_list_name, []))
        for obj_list_name in op_schema.id_fields
    ), "Some three-week old data in the archive"
    # Trigger another archival run
    event = {
        "data": base64.b64encode(json.dumps(params).encode()).decode()
    }
    main.kcidb_archive(event, None)
    # Check data_3w is now in the archive database
    dump = ar_client.dump()
    assert all(
        any(obj["id"] == "archive:3w"
            for obj in dump.get(obj_list_name, []))
        for obj_list_name in op_schema.id_fields
    ), "No complete three-week old data in the archive"

    # Empty the archive
    ar_client.empty()
    # Trigger a run of full archiving at once, and wait
    del params["data_max_duration"]
    event = {
        "data": base64.b64encode(json.dumps(params).encode()).decode()
    }
    main.kcidb_archive(event, None)
    # Check both data_4w and data_3w are in the archive database
    dump = ar_client.dump()
    assert all(
        any(obj["id"] == "archive:4w"
            for obj in dump.get(obj_list_name, [])) and
        any(obj["id"] == "archive:3w"
            for obj in dump.get(obj_list_name, []))
        for obj_list_name in op_schema.id_fields
    ), "No complete four- and three-week old data in the archive"
