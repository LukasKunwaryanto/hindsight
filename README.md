# Hindsight

Archives what customers saw on Nordic Nest Group's sites and ties each banner to its performance.
See the build reference doc for architecture and decisions.

## Phase 0: capture test

Actions → **Capture test** → **Run workflow**. The run summary shows whether nordicnest.se loaded
normally on desktop and mobile or was blocked by bot protection. Screenshots and captured HTML are
attached to the run as the `capture-test` artifact.

## Sample metrics (until BigQuery access is in place)

Actions → **Sample metrics** → **Run workflow** fills `promotion_metrics_daily`, `page_metrics_daily`,
`segment_metrics_daily` and `ab_test_metrics_daily` with sample numbers for the promotions on the real
captures (`tools/mock_metrics.py`). It also runs daily. Rows are marked `source = 'mock'`; the BigQuery
sync will write the same tables with `source = 'bigquery'`, so the app doesn't change when real data arrives.
