import os
from concurrent.futures import ThreadPoolExecutor
from time import sleep
import pytest
from unittest.mock import patch
from typing import List

from dlt.common.exceptions import TerminalException, TerminalValueError
from dlt.common.storages import FileStorage, LoadStorage, PackageStorage, ParsedLoadJobFileName
from dlt.common.storages.load_storage import JobWithUnsupportedWriterException
from dlt.common.destination.reference import LoadJob, TDestination

from dlt.load import Load
from dlt.destinations.job_impl import EmptyLoadJob

from dlt.destinations import dummy
from dlt.destinations.impl.dummy import dummy as dummy_impl
from dlt.destinations.impl.dummy.configuration import DummyClientConfiguration
from dlt.load.exceptions import LoadClientJobFailed, LoadClientJobRetry
from dlt.common.schema.utils import get_top_level_table

from tests.utils import (
    clean_test_storage,
    init_test_logging,
    TEST_DICT_CONFIG_PROVIDER,
    preserve_environ,
)
from tests.load.utils import prepare_load_package
from tests.utils import skip_if_not_active

skip_if_not_active("dummy")

NORMALIZED_FILES = [
    "event_user.839c6e6b514e427687586ccc65bf133f.0.jsonl",
    "event_loop_interrupted.839c6e6b514e427687586ccc65bf133f.0.jsonl",
]


@pytest.fixture(autouse=True)
def storage() -> FileStorage:
    return clean_test_storage(init_normalize=True, init_loader=True)


@pytest.fixture(scope="module", autouse=True)
def logger_autouse() -> None:
    init_test_logging()


def test_spool_job_started() -> None:
    # default config keeps the job always running
    load = setup_loader()
    load_id, schema = prepare_load_package(load.load_storage, NORMALIZED_FILES)
    files = load.load_storage.normalized_packages.list_new_jobs(load_id)
    assert len(files) == 2
    jobs: List[LoadJob] = []
    for f in files:
        job = Load.w_spool_job(load, f, load_id, schema)
        assert type(job) is dummy_impl.LoadDummyJob
        assert job.state() == "running"
        assert load.load_storage.normalized_packages.storage.has_file(
            load.load_storage.normalized_packages.get_job_file_path(
                load_id, PackageStorage.STARTED_JOBS_FOLDER, job.file_name()
            )
        )
        jobs.append(job)
    # still running
    remaining_jobs = load.complete_jobs(load_id, jobs, schema)
    assert len(remaining_jobs) == 2


def test_unsupported_writer_type() -> None:
    load = setup_loader()
    load_id, _ = prepare_load_package(
        load.load_storage, ["event_bot.181291798a78198.0.unsupported_format"]
    )
    with pytest.raises(TerminalValueError):
        load.load_storage.list_new_jobs(load_id)


def test_unsupported_write_disposition() -> None:
    load = setup_loader()
    load_id, schema = prepare_load_package(load.load_storage, [NORMALIZED_FILES[0]])
    # mock unsupported disposition
    schema.get_table("event_user")["write_disposition"] = "skip"
    # write back schema
    load.load_storage.normalized_packages.save_schema(load_id, schema)
    with ThreadPoolExecutor() as pool:
        load.run(pool)
    # job with unsupported write disp. is failed
    exception_file = [
        f
        for f in load.load_storage.normalized_packages.list_failed_jobs(load_id)
        if f.endswith(".exception")
    ][0]
    assert (
        "LoadClientUnsupportedWriteDisposition"
        in load.load_storage.normalized_packages.storage.load(exception_file)
    )


def test_get_new_jobs_info() -> None:
    load = setup_loader()
    load_id, schema = prepare_load_package(load.load_storage, NORMALIZED_FILES)

    # no write disposition specified - get all new jobs
    assert len(load.get_new_jobs_info(load_id)) == 2


def test_get_completed_table_chain_single_job_per_table() -> None:
    load = setup_loader()
    load_id, schema = prepare_load_package(load.load_storage, NORMALIZED_FILES)

    top_job_table = get_top_level_table(schema.tables, "event_user")
    assert load.get_completed_table_chain(load_id, schema, top_job_table) is None
    # fake being completed
    assert (
        len(
            load.get_completed_table_chain(
                load_id,
                schema,
                top_job_table,
                "event_user.839c6e6b514e427687586ccc65bf133f.jsonl",
            )
        )
        == 1
    )
    # actually complete
    loop_top_job_table = get_top_level_table(schema.tables, "event_loop_interrupted")
    load.load_storage.normalized_packages.start_job(
        load_id, "event_loop_interrupted.839c6e6b514e427687586ccc65bf133f.0.jsonl"
    )
    assert load.get_completed_table_chain(load_id, schema, loop_top_job_table) is None
    load.load_storage.normalized_packages.complete_job(
        load_id, "event_loop_interrupted.839c6e6b514e427687586ccc65bf133f.0.jsonl"
    )
    assert load.get_completed_table_chain(load_id, schema, loop_top_job_table) == [
        schema.get_table("event_loop_interrupted")
    ]
    assert load.get_completed_table_chain(
        load_id, schema, loop_top_job_table, "event_user.839c6e6b514e427687586ccc65bf133f.0.jsonl"
    ) == [schema.get_table("event_loop_interrupted")]


def test_spool_job_failed() -> None:
    # this config fails job on start
    load = setup_loader(client_config=DummyClientConfiguration(fail_prob=1.0))
    load_id, schema = prepare_load_package(load.load_storage, NORMALIZED_FILES)
    files = load.load_storage.normalized_packages.list_new_jobs(load_id)
    jobs: List[LoadJob] = []
    for f in files:
        job = Load.w_spool_job(load, f, load_id, schema)
        assert type(job) is EmptyLoadJob
        assert job.state() == "failed"
        assert load.load_storage.normalized_packages.storage.has_file(
            load.load_storage.normalized_packages.get_job_file_path(
                load_id, PackageStorage.STARTED_JOBS_FOLDER, job.file_name()
            )
        )
        jobs.append(job)
    # complete files
    remaining_jobs = load.complete_jobs(load_id, jobs, schema)
    assert len(remaining_jobs) == 0
    for job in jobs:
        assert load.load_storage.normalized_packages.storage.has_file(
            load.load_storage.normalized_packages.get_job_file_path(
                load_id, PackageStorage.FAILED_JOBS_FOLDER, job.file_name()
            )
        )
        assert load.load_storage.normalized_packages.storage.has_file(
            load.load_storage.normalized_packages.get_job_file_path(
                load_id, PackageStorage.FAILED_JOBS_FOLDER, job.file_name() + ".exception"
            )
        )
    started_files = load.load_storage.normalized_packages.list_started_jobs(load_id)
    assert len(started_files) == 0

    # test the whole flow
    load = setup_loader(client_config=DummyClientConfiguration(fail_prob=1.0))
    load_id, schema = prepare_load_package(load.load_storage, NORMALIZED_FILES)
    run_all(load)
    package_info = load.load_storage.get_load_package_info(load_id)
    assert package_info.state == "loaded"
    # all jobs failed
    assert len(package_info.jobs["failed_jobs"]) == 2


def test_spool_job_failed_exception_init() -> None:
    # this config fails job on start
    os.environ["LOAD__RAISE_ON_FAILED_JOBS"] = "true"
    os.environ["FAIL_IN_INIT"] = "true"
    load = setup_loader(client_config=DummyClientConfiguration(fail_prob=1.0))
    load_id, _ = prepare_load_package(load.load_storage, NORMALIZED_FILES)
    with patch.object(dummy_impl.DummyClient, "complete_load") as complete_load:
        with pytest.raises(LoadClientJobFailed) as py_ex:
            run_all(load)
        assert py_ex.value.load_id == load_id
        package_info = load.load_storage.get_load_package_info(load_id)
        assert package_info.state == "aborted"
        # both failed - we wait till the current loop is completed and then raise
        assert len(package_info.jobs["failed_jobs"]) == 2
        assert len(package_info.jobs["started_jobs"]) == 0
        # load id was never committed
        complete_load.assert_not_called()


def test_spool_job_failed_exception_complete() -> None:
    # this config fails job on start
    os.environ["LOAD__RAISE_ON_FAILED_JOBS"] = "true"
    os.environ["FAIL_IN_INIT"] = "false"
    load = setup_loader(client_config=DummyClientConfiguration(fail_prob=1.0))
    load_id, _ = prepare_load_package(load.load_storage, NORMALIZED_FILES)
    with pytest.raises(LoadClientJobFailed) as py_ex:
        run_all(load)
    assert py_ex.value.load_id == load_id
    package_info = load.load_storage.get_load_package_info(load_id)
    assert package_info.state == "aborted"
    # both failed - we wait till the current loop is completed and then raise
    assert len(package_info.jobs["failed_jobs"]) == 2
    assert len(package_info.jobs["started_jobs"]) == 0


def test_spool_job_retry_new() -> None:
    # this config retries job on start (transient fail)
    load = setup_loader(client_config=DummyClientConfiguration(retry_prob=1.0))
    load_id, schema = prepare_load_package(load.load_storage, NORMALIZED_FILES)
    files = load.load_storage.normalized_packages.list_new_jobs(load_id)
    for f in files:
        job = Load.w_spool_job(load, f, load_id, schema)
        assert job.state() == "retry"


def test_spool_job_retry_spool_new() -> None:
    # this config retries job on start (transient fail)
    load = setup_loader(client_config=DummyClientConfiguration(retry_prob=1.0))
    load_id, schema = prepare_load_package(load.load_storage, NORMALIZED_FILES)
    # call higher level function that returns jobs and counts
    with ThreadPoolExecutor() as pool:
        load.pool = pool
        jobs_count, jobs = load.spool_new_jobs(load_id, schema)
        assert jobs_count == 2
        assert len(jobs) == 2


def test_spool_job_retry_started() -> None:
    # this config keeps the job always running
    load = setup_loader()
    # dummy_impl.CLIENT_CONFIG = DummyClientConfiguration
    load_id, schema = prepare_load_package(load.load_storage, NORMALIZED_FILES)
    files = load.load_storage.normalized_packages.list_new_jobs(load_id)
    jobs: List[LoadJob] = []
    for f in files:
        job = Load.w_spool_job(load, f, load_id, schema)
        assert type(job) is dummy_impl.LoadDummyJob
        assert job.state() == "running"
        assert load.load_storage.normalized_packages.storage.has_file(
            load.load_storage.normalized_packages.get_job_file_path(
                load_id, PackageStorage.STARTED_JOBS_FOLDER, job.file_name()
            )
        )
        # mock job config to make it retry
        job.config.retry_prob = 1.0
        jobs.append(job)
    files = load.load_storage.normalized_packages.list_new_jobs(load_id)
    assert len(files) == 0
    # should retry, that moves jobs into new folder
    remaining_jobs = load.complete_jobs(load_id, jobs, schema)
    assert len(remaining_jobs) == 0
    # clear retry flag
    dummy_impl.JOBS = {}
    files = load.load_storage.normalized_packages.list_new_jobs(load_id)
    assert len(files) == 2
    # parse the new job names
    for fn in load.load_storage.normalized_packages.list_new_jobs(load_id):
        # we failed when already running the job so retry count will increase
        assert ParsedLoadJobFileName.parse(fn).retry_count == 1
    for f in files:
        job = Load.w_spool_job(load, f, load_id, schema)
        assert job.state() == "running"


def test_try_retrieve_job() -> None:
    load = setup_loader()
    load_id, schema = prepare_load_package(load.load_storage, NORMALIZED_FILES)
    # manually move jobs to started
    files = load.load_storage.normalized_packages.list_new_jobs(load_id)
    for f in files:
        load.load_storage.normalized_packages.start_job(
            load_id, FileStorage.get_file_name_from_file_path(f)
        )
    # dummy client may retrieve jobs that it created itself, jobs in started folder are unknown
    # and returned as terminal
    with load.destination.client(schema, load.initial_client_config) as c:
        job_count, jobs = load.retrieve_jobs(c, load_id)
        assert job_count == 2
        for j in jobs:
            assert j.state() == "failed"
    # new load package
    load_id, schema = prepare_load_package(load.load_storage, NORMALIZED_FILES)
    load.pool = ThreadPoolExecutor()
    jobs_count, jobs = load.spool_new_jobs(load_id, schema)
    assert jobs_count == 2
    # now jobs are known
    with load.destination.client(schema, load.initial_client_config) as c:
        job_count, jobs = load.retrieve_jobs(c, load_id)
        assert job_count == 2
        for j in jobs:
            assert j.state() == "running"


def test_completed_loop() -> None:
    load = setup_loader(client_config=DummyClientConfiguration(completed_prob=1.0))
    assert_complete_job(load)


def test_failed_loop() -> None:
    # ask to delete completed
    load = setup_loader(
        delete_completed_jobs=True, client_config=DummyClientConfiguration(fail_prob=1.0)
    )
    # actually not deleted because one of the jobs failed
    assert_complete_job(load, should_delete_completed=False)


def test_completed_loop_with_delete_completed() -> None:
    load = setup_loader(client_config=DummyClientConfiguration(completed_prob=1.0))
    load.load_storage = load.create_storage(is_storage_owner=False)
    load.load_storage.config.delete_completed_jobs = True
    assert_complete_job(load, should_delete_completed=True)


def test_retry_on_new_loop() -> None:
    # test job that retries sitting in new jobs
    load = setup_loader(client_config=DummyClientConfiguration(retry_prob=1.0))
    load_id, schema = prepare_load_package(load.load_storage, NORMALIZED_FILES)
    with ThreadPoolExecutor() as pool:
        # 1st retry
        load.run(pool)
        files = load.load_storage.normalized_packages.list_new_jobs(load_id)
        assert len(files) == 2
        # 2nd retry
        load.run(pool)
        files = load.load_storage.normalized_packages.list_new_jobs(load_id)
        assert len(files) == 2

        # jobs will be completed
        load = setup_loader(client_config=DummyClientConfiguration(completed_prob=1.0))
        load.run(pool)
        files = load.load_storage.normalized_packages.list_new_jobs(load_id)
        assert len(files) == 0
        # complete package
        load.run(pool)
        assert not load.load_storage.normalized_packages.storage.has_folder(
            load.load_storage.get_normalized_package_path(load_id)
        )
        # parse the completed job names
        completed_path = load.load_storage.loaded_packages.get_package_path(load_id)
        for fn in load.load_storage.loaded_packages.storage.list_folder_files(
            os.path.join(completed_path, PackageStorage.COMPLETED_JOBS_FOLDER)
        ):
            # we update a retry count in each case
            assert ParsedLoadJobFileName.parse(fn).retry_count == 2


def test_retry_exceptions() -> None:
    load = setup_loader(client_config=DummyClientConfiguration(retry_prob=1.0))
    prepare_load_package(load.load_storage, NORMALIZED_FILES)
    with ThreadPoolExecutor() as pool:
        # 1st retry
        with pytest.raises(LoadClientJobRetry) as py_ex:
            while True:
                load.run(pool)
        # configured to retry 5 times before exception
        assert py_ex.value.max_retry_count == py_ex.value.retry_count == 5

        # we can do it again
        with pytest.raises(LoadClientJobRetry) as py_ex:
            while True:
                load.run(pool)
        # this continues retry
        assert py_ex.value.max_retry_count * 2 == py_ex.value.retry_count == 10


def test_load_single_thread() -> None:
    os.environ["LOAD__WORKERS"] = "1"
    load = setup_loader(client_config=DummyClientConfiguration(completed_prob=1.0))
    assert load.config.pool_type == "none"
    load_id, _ = prepare_load_package(load.load_storage, NORMALIZED_FILES)
    # we do not need pool to complete
    metrics = load.run(None)
    while metrics.pending_items > 0:
        metrics = load.run(None)
    assert not load.load_storage.storage.has_folder(
        load.load_storage.get_normalized_package_path(load_id)
    )


def test_wrong_writer_type() -> None:
    load = setup_loader()
    load_id, _ = prepare_load_package(
        load.load_storage,
        [
            "event_bot.b1d32c6660b242aaabbf3fc27245b7e6.0.insert_values",
            "event_user.b1d32c6660b242aaabbf3fc27245b7e6.0.insert_values",
        ],
    )
    with ThreadPoolExecutor() as pool:
        with pytest.raises(JobWithUnsupportedWriterException) as exv:
            load.run(pool)
    assert exv.value.load_id == load_id


def test_terminal_exceptions() -> None:
    try:
        raise TerminalValueError("a")
    except TerminalException:
        assert True
    else:
        raise AssertionError()


def assert_complete_job(load: Load, should_delete_completed: bool = False) -> None:
    load_id, _ = prepare_load_package(load.load_storage, NORMALIZED_FILES)
    # will complete all jobs
    with patch.object(dummy_impl.DummyClient, "complete_load") as complete_load:
        with ThreadPoolExecutor() as pool:
            load.run(pool)
            # did process schema update
            assert load.load_storage.storage.has_file(
                os.path.join(
                    load.load_storage.get_normalized_package_path(load_id),
                    PackageStorage.APPLIED_SCHEMA_UPDATES_FILE_NAME,
                )
            )
            # will finalize the whole package
            load.run(pool)
            # moved to loaded
            assert not load.load_storage.storage.has_folder(
                load.load_storage.get_normalized_package_path(load_id)
            )
            completed_path = load.load_storage.loaded_packages.get_job_folder_path(
                load_id, "completed_jobs"
            )
            if should_delete_completed:
                # package was deleted
                assert not load.load_storage.loaded_packages.storage.has_folder(completed_path)
            else:
                # package not deleted
                assert load.load_storage.loaded_packages.storage.has_folder(completed_path)
            # complete load on client was called
            complete_load.assert_called_once_with(load_id)


def run_all(load: Load) -> None:
    pool = ThreadPoolExecutor()
    while True:
        metrics = load.run(pool)
        if metrics.pending_items == 0:
            return
        sleep(0.1)


def setup_loader(
    delete_completed_jobs: bool = False, client_config: DummyClientConfiguration = None
) -> Load:
    # reset jobs for a test
    dummy_impl.JOBS = {}
    destination: TDestination = dummy()  # type: ignore[assignment]
    client_config = client_config or DummyClientConfiguration(loader_file_format="jsonl")
    # patch destination to provide client_config
    # destination.client = lambda schema: dummy_impl.DummyClient(schema, client_config)

    # setup loader
    with TEST_DICT_CONFIG_PROVIDER().values({"delete_completed_jobs": delete_completed_jobs}):
        return Load(destination, initial_client_config=client_config)
