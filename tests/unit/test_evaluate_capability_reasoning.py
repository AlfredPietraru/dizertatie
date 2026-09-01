from helper_scripts.evaluate_capability_reasoning import _expected, _metrics


def test_expected_reads_synthetic_capability_contract() -> None:
    result = _expected({
        "expected_capability_interpretation": {
            "status": "valid",
            "capabilities": {
                "mapping": "cartographer", "navigation": "nav2",
                "exploration": "explore_lite", "odometry": "kiss_icp",
            },
        },
    })
    assert result == {
        "status": "valid",
        "capabilities": {
            "mapping": "cartographer", "navigation": "nav2",
            "exploration": "explore_lite", "odometry": "kiss_icp",
        },
    }


def test_metrics_count_exact_status_and_field_quality() -> None:
    records = [
        {
            "source_seed": "seed-1", "exact": True, "status_correct": True, "error": None,
            "expected_fields": {"mapping": "cartographer"},
            "predicted_fields": {"mapping": "cartographer"},
            "correct_fields": ["mapping"],
        },
        {
            "source_seed": "seed-1", "exact": False, "status_correct": True, "error": None,
            "expected_fields": {"odometry": "kiss_icp"},
            "predicted_fields": {"odometry": "kinematic_icp"},
            "correct_fields": [],
        },
    ]
    result = _metrics(records)
    assert result["exact_capability_accuracy"] == 0.5
    assert result["status_accuracy"] == 1.0
    assert result["field_micro_precision"] == 0.5
    assert result["field_micro_recall"] == 0.5
    assert result["by_source_seed"]["seed-1"]["exact_accuracy"] == 0.5
