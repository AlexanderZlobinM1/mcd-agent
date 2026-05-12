from mcd_agent import daemon


def test_page_hits_cleanup_runtime_keys_are_stable() -> None:
    expected = {
        "enable_page_hits_orphan_cleanup",
        "page_hits_orphan_cleanup_interval_sec",
        "page_hits_orphan_cleanup_quiet_hour",
        "page_hits_orphan_cleanup_quiet_window_min",
        "page_hits_orphan_cleanup_batch_size",
        "page_hits_orphan_cleanup_batches_per_run",
        "page_hits_orphan_cleanup_sleep_sec",
        "page_hits_orphan_cleanup_grace_min",
        "page_hits_orphan_cleanup_max_run_sec",
    }

    assert expected <= daemon._STABLE_RUNTIME_KEYS
