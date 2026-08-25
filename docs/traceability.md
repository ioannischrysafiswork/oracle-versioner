# Traceability Matrix

Every requirement in [`requirements.md`](requirements.md) mapped to the module
that implements it and the test that proves it, as
[`architecture.md`](architecture.md) §24 requires.

**Status key:** ✅ implemented and tested · ⚠️ implemented with a documented
deviation · ⛔ deliberately not implemented.

Test counts are per file, from `python -m pytest`. Totals: **627 tests**, all
offline — no Oracle, no network, no GitLab.

---

## §1-§2 Role, objective, workflow

| Requirement | Module | Test | Status |
|---|---|---|---|
| Ten-step workflow, in order | `workflow/pipeline.py`, `workflow/run_unit.py`, `workflow/git_phase.py` | `test_pipeline.py::test_full_git_phase_commits_pushes_then_advances_state` | ✅ |
| Stop on critical failure | `main.py` exit codes | `test_skeleton.py::test_invalid_configuration_exits_two` | ✅ |
| Metadata never updated unless push succeeded | `workflow/git_phase.py::_persist_state` | `test_pipeline.py::test_state_is_not_advanced_when_the_push_fails` | ✅ |
| No comparison against existing file content | `repository/comparer.py` | `test_repository.py::test_reconciler_compares_paths_not_contents` | ✅ |
| Oracle never written to | `oracle/sql.py::assert_read_only` | `test_connection.py::test_every_sql_constant_is_read_only` (16 statements) | ✅ |

## §3 Database connections

| Requirement | Module | Test | Status |
|---|---|---|---|
| Any number of databases | `config/loader.py` | `test_config.py::test_valid_database_parses` | ✅ |
| Name, DSN, user, password, git folder | `config/models.py::DatabaseConfig` | `test_config.py` (38 tests) | ✅ |
| Password never hardcoded | `config/secrets.py` | `test_config.py::test_secret_values_never_enter_the_configuration` | ✅ |
| Connection failure does not stop other databases | `workflow/run_unit.py::run` | `test_pipeline.py::test_one_failed_database_does_not_stop_the_others` | ✅ |
| Connection pooling | `oracle/connection.py::ConnectionFactory` | — (needs a live instance) | ⚠️ |

## §4 Load metadata

| Requirement | Module | Test | Status |
|---|---|---|---|
| Read `execution_state.json` | `state/execution_state.py::load` | `test_state.py::test_save_then_load_round_trips` | ✅ |
| Missing file ⇒ full export | `state/models.py::watermark_for` | `test_state.py::test_missing_file_means_full_export` | ✅ |
| Created after first success | `workflow/git_phase.py::_persist_state` | `test_pipeline.py::test_full_git_phase_commits_pushes_then_advances_state` | ✅ |
| Corrupt file is fatal, never recreated | `state/execution_state.py::load` | `test_state.py::test_corrupt_file_is_fatal_and_left_untouched` | ✅ |

## §5 Find changed objects

| Requirement | Module | Test | Status |
|---|---|---|---|
| `DBA_OBJECTS`, `LAST_DDL_TIME > last_successful_run` | `oracle/sql.py::build_discovery_query` | `test_extractor.py::test_incremental_query_binds_the_watermark` | ✅ |
| All twelve object types | `models/oracle_object.py::ObjectType` | `test_config.py::test_default_object_type_filter_covers_all_twelve_types` | ✅ |
| Only VALID and INVALID | `oracle/sql.py::_STATIC_PREDICATES` | `test_extractor.py::test_discovery_query_carries_the_required_predicates` | ✅ |
| Ignore Oracle internal schemas | `oracle/sql.py::INTERNAL_SCHEMAS` (38 schemas) | `test_extractor.py::test_internal_schemas_are_always_excluded` | ✅ |
| Ignore Oracle-maintained objects | `oracle/sql.py` (12c+ column, version-gated) | `test_extractor.py::test_oracle_maintained_is_omitted_before_12c` | ✅ |
| Include/exclude schema filters | `config/validation.py::_schema_filter` | `test_extractor.py::test_include_filter_is_applied_in_sql` | ✅ |
| Sort by owner, type, name | `oracle/sql.py` `ORDER BY` | `test_extractor.py::test_discovery_query_carries_the_required_predicates` | ✅ |

## §6 Extract source

| Requirement | Module | Test | Status |
|---|---|---|---|
| PL/SQL types via `DBA_SOURCE` | `oracle/source_extractor.py::SourceStrategy` | `test_extractor.py::test_source_lines_are_joined_in_retrieved_order` | ✅ |
| **VIEW via `DBA_SOURCE`** | `oracle/source_extractor.py::ViewStrategy` | `test_extractor.py::test_view_is_extracted_from_dba_views` | ⚠️ **D-7** |
| TABLE via `DBA_TAB_COLUMNS` | `oracle/source_extractor.py::ColumnStrategy` | `test_extractor.py::test_table_columns_render_in_column_id_order` | ✅ |
| SEQUENCE, MV via `DBMS_METADATA` | `oracle/ddl_extractor.py::MetadataStrategy` | `test_extractor.py::test_metadata_extraction_reads_the_full_clob` | ✅ |
| SYNONYM via `DBA_SYNONYMS` | `oracle/source_extractor.py::DictionaryStrategy` | `test_extractor.py::test_synonym_rendering` | ✅ |
| Never concatenate out of order; always `ORDER BY LINE` | `oracle/sql.py::SELECT_SOURCE` | `test_extractor.py::test_source_extraction_orders_by_line` | ✅ |
| No truncation | `oracle/connection.py::fetch_value` (full CLOB read) | `test_extractor.py::test_long_source_is_not_truncated` (5000 lines) | ✅ |

## §7 Normalize

| Requirement | Module | Test | Status |
|---|---|---|---|
| Trailing spaces, line endings, tabs, blank lines | `normalizer/ddl_normalizer.py` | `test_normalizer.py` (147 tests) | ✅ |
| `CREATE OR REPLACE` formatting | `ddl_normalizer.py::_normalize_header` | `test_normalizer.py::test_golden_file` | ✅ |
| Quoted identifiers where possible | `plsql_normalizer.py::normalize_quoted_identifiers` | `test_normalizer.py::test_quoted_identifiers_are_unquoted_only_when_safe` | ✅ |
| Keyword casing | `plsql_normalizer.py::normalize_keyword_case` | `test_normalizer.py::test_keyword_casing_does_not_touch_comments_or_literals` | ✅ |
| Remove all listed storage clauses | `storage_clause_remover.py` | `test_normalizer.py::test_every_specified_storage_clause_is_covered` | ⚠️ **D-8** |
| Do not modify PL/SQL logic | `plsql_normalizer.py::scan_regions` | `test_normalizer.py::test_normalization_preserves_the_code_token_stream` | ✅ |
| Deterministic; twice ⇒ identical | `ddl_normalizer.py` | `test_normalizer.py::test_normalization_is_idempotent` + 1500-case fuzz | ✅ |

## §8 Write to repository

| Requirement | Module | Test | Status |
|---|---|---|---|
| One object ⇒ one `.sql` file | `repository/file_mapper.py::to_path` | `test_repository.py::test_every_type_maps_to_the_expected_path` | ✅ |
| Directory layout | `file_mapper.py::FOLDER_BY_TYPE` | `test_repository.py::test_folder_names_match_the_specification` | ✅ |
| Replace existing file | `repository/writer.py::FileWriter` | `test_repository.py::test_writer_overwrites_rather_than_appends` | ✅ |
| No content comparison | `repository/comparer.py` | `test_repository.py::test_reconciler_compares_paths_not_contents` | ✅ |

## §9-§10 Git operations and push

| Requirement | Module | Test | Status |
|---|---|---|---|
| Only files written are staged | `git/git_client.py::add` | `test_git.py::test_only_named_paths_are_staged` | ✅ |
| Commit message format | `git/commit.py::build_message` | `test_git.py::test_commit_message_format` | ✅ |
| Body: time, count, object list, previous SHA, timestamp | `git/commit.py` | `test_git.py::test_commit_message_format` | ✅ |
| Never create empty commits | `git/commit.py::has_staged_changes` | `test_git.py::test_rewriting_identical_content_stages_nothing` | ✅ |
| Push; on failure abort and do not update metadata | `git/push.py`, `workflow/git_phase.py` | `test_pipeline.py::test_state_is_not_advanced_when_the_push_fails` | ✅ |

## §11 Save metadata

| Requirement | Module | Test | Status |
|---|---|---|---|
| Only after successful push | `workflow/git_phase.py::_persist_state` | `test_pipeline.py::test_state_is_not_advanced_when_the_push_fails` | ✅ |
| All six recorded fields | `state/models.py::DatabaseState` | `test_state.py::test_round_trip_preserves_every_field` | ✅ |
| Committed with the source changes | `workflow/git_phase.py` (amend) | `test_pipeline.py::test_full_git_phase_commits_pushes_then_advances_state` | ✅ |

## §12-§13 Error handling and logging

| Requirement | Module | Test | Status |
|---|---|---|---|
| Seven per-object statuses | `models/oracle_object.py::ObjectStatus` | `test_pipeline.py::test_unmappable_name_fails_only_its_own_object` | ✅ |
| Failures never stop the application | `workflow/run_unit.py::_process_one` | `test_pipeline.py::test_normalization_failure_is_contained_to_its_object` | ✅ |
| Structured logs with the required fields | `logging/logger.py::JsonFormatter` | `test_logging.py::test_json_records_carry_the_required_fields` | ✅ |
| Never log passwords or credential DSNs | `logging/logger.py::RedactionFilter` | `test_logging.py::test_sentinel_never_appears_in_a_traceback` | ✅ |

## §14 Performance

| Requirement | Module | Test | Status |
|---|---|---|---|
| Connection pooling | `oracle/connection.py` | — (needs a live instance) | ⚠️ |
| Fetch in batches | `oracle/connection.py::_execute` (arraysize/prefetchrows) | — (needs a live instance) | ⚠️ |
| Never query the same object twice | `oracle/object_discovery.py::_map` | `test_extractor.py::test_discovery_deduplicates_by_owner_type_name` | ✅ |
| Databases concurrently | `workflow/pipeline.py::_extraction_phase` | `test_pipeline.py::test_every_configured_database_produces_a_run_unit` | ✅ |
| Objects within a database sequentially | `workflow/run_unit.py::_process` | — (structural: a plain loop) | ✅ |

## §15-§16 Security and reliability

| Requirement | Module | Test | Status |
|---|---|---|---|
| Never hardcode credentials | `config/secrets.py` | `test_config.py::test_secrets_are_resolved_and_handed_to_the_redaction_sink` | ✅ |
| Vault / Secrets Manager support | `config/secrets.py::SecretProvider` (protocol) | `test_config.py` (fake provider substituted) | ⚠️ only the env provider ships |
| Validate extracted source before writing | `oracle/metadata.py::validate` | `test_extractor.py::test_whitespace_only_source_is_rejected` | ✅ |
| Validate Git status before committing | `git/commit.py::has_staged_changes` | `test_git.py::test_nothing_staged_means_no_commit` | ✅ |
| Never lose previous state | `utils/filesystem.py::atomic_write_text` | `test_utils.py::test_failed_write_cleans_up_and_preserves_the_original` | ✅ |

## §17-§18 Layout and code quality

| Requirement | Evidence | Status |
|---|---|---|
| Repository layout as specified | Directory tree | ⚠️ **D-9** (eight added modules) |
| Python 3.13+, PEP 8, type hints, dataclasses | `ruff check` and `mypy` both clean over 45 files | ✅ |
| Unit tests | 627 tests | ✅ |
| Every public function documented | ruff `D` rules enforced | ✅ |
| No module beyond ~300 lines | `test_skeleton.py::test_no_module_exceeds_the_size_limit` | ✅ |

## §19-§21 GitLab and Docker

| Requirement | Module | Test | Status |
|---|---|---|---|
| Scheduled and manual execution | `.gitlab-ci.yml` `sync` | `test_packaging.py::test_sync_cannot_be_triggered_by_a_push` | ✅ |
| No permanently running server | One process per execution | `test_skeleton.py::test_application_runs_end_to_end_and_exits_zero` | ✅ |
| Stateless; all state in Git | `state/execution_state.py` | `test_state.py` (27 tests) | ✅ |
| Pipeline fails on the five listed conditions | `main.py` exit codes | `test_skeleton.py`, `test_pipeline.py` | ✅ |
| Pipeline passes when nothing changed | `git/commit.py` empty-commit gate | `test_pipeline.py::test_unchanged_second_run_creates_no_commit` | ✅ |
| Image: Python 3.13, oracledb, git | `Dockerfile` | `test_packaging.py` (10 tests) + image built and run | ✅ |
| Container runs once and exits | `Dockerfile` ENTRYPOINT | Verified against the built image | ✅ |

## §22-§24 State, multi-database, constraints

| Requirement | Module | Test | Status |
|---|---|---|---|
| State updated only on push success | `workflow/git_phase.py` | `test_pipeline.py::test_state_is_not_advanced_when_the_push_fails` | ✅ |
| Never updated when nothing changed | `workflow/git_phase.py` | `test_pipeline.py::test_unchanged_second_run_creates_no_commit` | ✅ |
| Each database its own state | `state/models.py::ExecutionState` | `test_state.py::test_with_database_does_not_mutate_the_original` | ✅ |
| Dry-run mode | `repository/writer.py::NullWriter` | `test_pipeline.py::test_dry_run_writes_nothing` | ✅ |
| No business logic in `main.py` | `main.py` (CLI, wiring, exit code only) | Structural | ✅ |
| Dependency injection | Every collaborator is a constructor argument | The fakes in `conftest.py` | ✅ |

---

## Behavioural scenarios (architecture §22)

| Scenario | Test |
|---|---|
| §22.1 Object created | `test_pipeline.py::test_objects_are_written_to_their_mapped_paths` |
| §22.2 Object altered | `test_git.py::test_changed_content_does_stage` |
| §22.3a Unchanged, no DDL activity | `test_extractor.py::test_incremental_query_binds_the_watermark` |
| §22.3b `LAST_DDL_TIME` moved, source did not | `test_pipeline.py::test_unchanged_second_run_creates_no_commit` |
| §22.4 Object dropped | `test_pipeline.py::test_dropped_object_file_is_reported_for_deletion` |
| §22.5 Extraction fails | `test_pipeline.py::test_unmappable_name_fails_only_its_own_object` |
| §22.6 Oracle unavailable | `test_pipeline.py::test_one_failed_database_does_not_stop_the_others` |
| §22.7 Push fails | `test_pipeline.py::test_state_is_not_advanced_when_the_push_fails` |

## Approved decisions

| ID | Decision | Where | Test |
|---|---|---|---|
| D-1 | Reconciliation every execution | `workflow/run_unit.py::_reconcile` | `test_pipeline.py::test_dropped_object_file_is_reported_for_deletion` |
| D-2 | `ALTER SESSION` permitted | `oracle/sql.py::SESSION_INITIALIZATION` | `test_connection.py::test_session_initialization_is_read_only` |
| D-3 | Amend + `--force-with-lease` | `workflow/git_phase.py` | `test_git.py::test_the_lease_refuses_to_clobber_a_concurrent_push` |
| D-4 | `git` CLI via subprocess | `git/git_client.py` | `test_git.py` (37 tests, real repositories) |
| D-5 | Illegal names fail loudly | `repository/file_mapper.py::validate_name` | `test_repository.py::test_illegal_characters_fail_loudly` |
| D-6 | 10% mass-deletion threshold | `repository/comparer.py::exceeds_threshold` | `test_repository.py::test_mass_deletion_threshold` |

---

## Deviations requiring ratification

Each was flagged when introduced. Nothing here has been changed in
`requirements.md` or `architecture.md` — they are listed for a decision.

| ID | Deviation | Why | Reversible? |
|---|---|---|---|
| **D-7** | VIEW is extracted from `DBA_VIEWS`, not `DBA_SOURCE` | Oracle does not store view text in `DBA_SOURCE`. As specified, every view returns zero rows and is treated as dropped. `architecture.md` §21.2 already grants `SELECT` on `DBA_VIEWS` | Yes, but the result is a repository with no views |
| **D-8** | `CACHE <n>` is stripped from sequences | `requirements.md` §7 lists `CACHE` as a storage clause. On a sequence it is real configuration, so `CACHE 1000` silently becomes the default | Yes — exempt SEQUENCE from the rule |
| **D-9** | Eight modules added beyond §17 | `exceptions.py`, `config/secrets.py`, `config/validation.py`, `oracle/sql.py`, `utils/retry.py`, `workflow/{pipeline,run_unit,git_phase}.py`. Each is required by the architecture text but absent from the module list | Yes, at the cost of exceeding the ~300-line rule |
| **D-10** | Windows device names (`CON`, `NUL`, …) fail as unmappable | Such a file makes the *entire repository* un-checkout-able on Windows, not just that file | Yes |
| **D-11** | Case-insensitive path collisions fail loudly | Oracle allows `"Foo"` and `"FOO"`; a Windows or macOS filesystem cannot hold both. Undetected, one overwrites the other and reconciliation then deletes a live object's file | Yes |
| **D-12** | `ensure_branch` does not fast-forward to the remote tip | §11.2 places the fast-forward before staging, but extraction has already written files by then; a fast-forward could conflict with them. Divergence is handled by the rebase-and-retry path §11.6 defines | Yes |
| **D-13** | Reconciliation blocked by the threshold does not fail the run | §14.3 says extraction results still commit, so the exit code stays 0 despite an `ERROR` | Yes — make it exit 1 |
| **D-14** | `ruff` + `mypy` added as dev dependencies | §6 makes lint and type check blocking in CI; no toolchain is named. Dev-only, never in the runtime image | Yes, at the cost of dropping the gate |

## Known gaps

| Gap | Impact | Suggested phase |
|---|---|---|
| `START WITH <n>` on sequences is not stripped | `DBMS_METADATA` emits the *current* value, so an active sequence produces a commit on every run | Next |
| A materialized view's container table is exported twice | Oracle lists an MV's backing table in `DBA_OBJECTS` as a `TABLE`, so `MV_EMP` appears under both `materialized_views/` and `tables/`. Confirmed against a live instance. Filtering it means changing the §4.1 predicates | Next |
| Only the environment `SecretProvider` ships | Vault and Secrets Manager are interface-ready but unimplemented (`requirements.md` §15) | When needed |
| `SET TRANSACTION READ ONLY` not issued | Optional defence in depth (§3.3); grants are the real guarantee | Optional |

---

## Verified against a live Oracle instance

The integration suite (`tests/test_integration_oracle.py`, 23 tests, `-m oracle`)
has been run against Oracle Free 23c with sample objects of all twelve types, and
the full application has been run end to end against it: 12 objects extracted,
normalized, written, committed, and pushed; a second run discovered nothing and
created no commit.

It found two defects the offline suite could not, both now fixed and covered by
offline regression tests:

| Defect | Why fakes missed it | Consequence had it shipped |
|---|---|---|
| `SYSTIMESTAMP` returns a **naive** datetime, and `SESSIONTIMEZONE` is the *client's* offset while `DBTIMEZONE` is the *server's* | The fake returned a timezone-aware value, as the code assumed | The watermark would be stored in the wrong zone and bound back through a second wrong conversion. Against a database in another timezone, every change in the intervening hours is **silently skipped** — undetectable data loss |
| The commit message was rebuilt during the `--amend`, when only the state file was staged | No test inspected the *published* message, only the one passed to `commit()` | Every published commit carried `Number of objects : 0` and an empty object list — a specified requirement (§9) silently unmet |
