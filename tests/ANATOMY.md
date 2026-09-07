---
related_files:
  - ENVIRONMENT_VARIABLES.md
  - ANATOMY.md
  - CONTRACT.md
  - AGENTS.md
  - dev-guide-skill/SKILL.md
  - pyproject.toml
  - tests/BEHAVIORS.md
  - tests/CONTRACT.md
  - tests/__init__.py
  - tests/_agent_dir_helpers.py
  - tests/_agent_presence_helpers.py
  - tests/_chat_completion_helpers.py
  - tests/_daemon_helpers.py
  - tests/_detached_cli_parent.py
  - tests/_fake_codex_app_server.py
  - tests/_fake_codex_cli.py
  - tests/_fake_llm_adapter.py
  - tests/_fake_resume_cli.py
  - tests/_lifecycle_clock_helpers.py
  - tests/_manager_detached_parent.py
  - tests/_migration_workspace_helpers.py
  - tests/_molt_helpers.py
  - tests/_notification_helpers.py
  - tests/_notification_store_helpers.py
  - tests/_refresh_watcher_helpers.py
  - tests/_service_helpers.py
  - tests/_snapshot_helpers.py
  - tests/_workdir_lease_helpers.py
  - tests/codex
  - tests/conftest.py
  - tests/contracts/llm_conversation_input/__init__.py
  - tests/contracts/llm_conversation_input/regimes.py
  - tests/contracts/llm_conversation_input/test_openai_responses_tool_result_conversion.py
  - tests/contracts/llm_conversation_input/test_regime_inventory.py
  - tests/contracts/llm_conversation_input/test_send_str.py
  - tests/contracts/llm_conversation_input/test_send_tool_results.py
  - tests/integration_test_claude_code.py
  - tests/integration_test_kimi_code.py
  - tests/opencode
  - tests/test_activate_preset.py
  - tests/test_acp_stdio.py
  - tests/test_puffo_v0_profile.py
  - tests/test_provider_admission.py
  - tests/test_execution_workspace.py
  - tests/test_turn_events.py
  - tests/test_turn_permissions.py
  - tests/test_session_mcp.py
  - tests/test_active_stuck_watchdog.py
  - tests/test_adapter_registry.py
  - tests/test_aed_recovery.py
  - tests/test_aed_tool_pairing.py
  - tests/test_agent.py
  - tests/test_agent_capabilities.py
  - tests/test_agent_config_hydration.py
  - tests/test_agent_meta_guidance.py
  - tests/test_agent_presence.py
  - tests/test_agent_preset_manifest.py
  - tests/test_agent_readme.py
  - tests/test_agent_session.py
  - tests/test_agent_session_wiring.py
  - tests/test_anatomy_drift_checker.py
  - tests/test_anthropic_thinking_budget.py
  - tests/test_api_gate.py
  - tests/test_apriori_summary_executor.py
  - tests/test_apriori_summary_schema.py
  - tests/test_architecture_documents.py
  - tests/test_avatar_launcher.py
  - tests/test_avatar_launcher_windows.py
  - tests/test_avatar_preset_inheritance.py
  - tests/test_avatar_rules.py
  - tests/test_avatar_timezone_inheritance.py
  - tests/test_base_agent.py
  - tests/test_bash_async.py
  - tests/test_bash_async_process_contract.py
  - tests/test_bash_shell_dialect.py
  - tests/test_browser_capability.py
  - tests/test_browser_policy_cursor_edges.py
  - tests/test_browser_transport.py
  - tests/test_catalog_helpers.py
  - tests/test_chat_interface_invariant.py
  - tests/test_chat_interface_remove_pair_by_notif_id.py
  - tests/test_check_caps.py
  - tests/test_claude_code_adapter.py
  - tests/test_claude_live_effort.py
  - tests/test_claude_code_effort.py
  - tests/test_cli.py
  - tests/test_cli_daemon.py
  - tests/test_cli_integration.py
  - tests/test_cli_runtime_env.py
  - tests/test_cli_worker_poison_recovery.py
  - tests/test_cloud_mail_addon.py
  - tests/test_cloud_mail_toolfamily_ltpv2.py
  - tests/test_codex_account_source.py
  - tests/test_codex_endpoint_override.py
  - tests/test_codex_endpoint_pool.py
  - tests/test_codex_native_multiaccount.py
  - tests/test_codex_pool.py
  - tests/test_codex_prompt_cache_key.py
  - tests/test_codex_quota.py
  - tests/test_codex_raw_reasoning_replay.py
  - tests/test_codex_standalone_compaction.py
  - tests/test_codex_ws_delta.py
  - tests/test_codex_ws_session.py
  - tests/test_codex_ws_tool_result_freeze.py
  - tests/test_compaction.py
  - tests/test_config_resolve_jsonc.py
  - tests/test_correlated_turns.py
  - tests/test_context.py
  - tests/test_context_ownership_redesign.py
  - tests/test_context_pressure_reminder.py
  - tests/test_context_pressure_streak.py
  - tests/test_custom_responses_stateless.py
  - tests/test_daemon.py
  - tests/test_daemon_attention_delay.py
  - tests/test_daemon_backend_options.py
  - tests/test_daemon_check.py
  - tests/test_daemon_check_historical.py
  - tests/test_daemon_checkpoint.py
  - tests/test_daemon_claude_interactive_backend.py
  - tests/test_daemon_claude_p_background_guard.py
  - tests/test_daemon_claude_p_submanual.py
  - tests/test_daemon_claude_p_usage_backend.py
  - tests/test_daemon_claude_usage.py
  - tests/test_daemon_cli_143_attribution.py
  - tests/test_daemon_cli_watchdog_scope.py
  - tests/test_daemon_codex_usage.py
  - tests/test_daemon_common_finish_mcp.py
  - tests/test_daemon_cursor_backend.py
  - tests/test_daemon_cursor_submanual.py
  - tests/test_daemon_deepseek_submanual.py
  - tests/test_daemon_detached_supervisor.py
  - tests/test_daemon_email_mcp.py
  - tests/test_daemon_empty_parity.py
  - tests/test_daemon_kimicode_submanual.py
  - tests/test_daemon_lingtai_submanual.py
  - tests/test_daemon_manifest.py
  - tests/test_daemon_mimocode_jsonl.py
  - tests/test_daemon_missing_finish_guidance.py
  - tests/test_daemon_oh_my_pi_submanual.py
  - tests/test_daemon_opencode_backend.py
  - tests/test_daemon_opencode_submanual.py
  - tests/test_daemon_per_batch_limits.py
  - tests/test_daemon_preset_capabilities.py
  - tests/test_daemon_process_port.py
  - tests/test_daemon_qwen_code_submanual.py
  - tests/test_daemon_run_dir.py
  - tests/test_daemon_runtime_helpers.py
  - tests/test_daemon_terminal_state_gate.py
  - tests/test_daemon_windows_lock.py
  - tests/test_daemon_windows_process_port.py
  - tests/test_daemon_windows_supervisor.py
  - tests/test_deep_refresh.py
  - tests/test_deepseek_adapter.py
  - tests/test_deepseek_reasoning_effort.py
  - tests/test_docs_governance.py
  - tests/test_eigen.py
  - tests/test_email_abs_reply_route.py
  - tests/test_email_identity.py
  - tests/test_environment_variable_catalogue.py
  - tests/test_event_journal.py
  - tests/test_exit_code_interpretation.py
  - tests/test_expired_spill_messaging.py
  - tests/test_feishu_automatic_task_cards.py
  - tests/test_feishu_channel_events.py
  - tests/test_feishu_channel_sdk_account.py
  - tests/test_feishu_control_cards.py
  - tests/test_feishu_inbound_media.py
  - tests/test_feishu_interactive_cards.py
  - tests/test_feishu_native_progress.py
  - tests/test_feishu_notification_metadata.py
  - tests/test_feishu_programmable_task_cards.py
  - tests/test_feishu_reactions_errors.py
  - tests/test_feishu_rich_outbound.py
  - tests/test_feishu_rich_outbound_media.py
  - tests/test_feishu_stdio_logging.py
  - tests/test_feishu_toolfamily_ltpv2.py
  - tests/test_file_io_sidecar.py
  - tests/test_file_tool_family.py
  - tests/test_file_tool_plugin_package.py
  - tests/test_filesystem_mail.py
  - tests/test_event_journal_count_nudge.py
  - tests/test_folder_size_nudge.py
  - tests/test_fsutil.py
  - tests/test_gated_session_proxy.py
  - tests/test_git_init.py
  - tests/test_goal_notification.py
  - tests/test_handshake.py
  - tests/test_heartbeat.py
  - tests/test_how_to_change_name.py
  - tests/test_how_to_change_name_e2e.py
  - tests/test_i18n.py
  - tests/test_imap_account_metadata.py
  - tests/test_imap_empty_args.py
  - tests/test_imap_outlook_oauth.py
  - tests/test_imap_read_attachment_sanitization.py
  - tests/test_imap_reply_attachments.py
  - tests/test_imap_toolfamily_ltpv2.py
  - tests/test_inherit_fallback.py
  - tests/test_init_reader.py
  - tests/test_init_schema.py
  - tests/test_init_schema_procedures.py
  - tests/test_interactive_terminal_lifecycle.py
  - tests/test_interactive_terminal_port.py
  - tests/test_interface.py
  - tests/test_intrinsic_manual_actions.py
  - tests/test_intrinsics_comm.py
  - tests/test_karma.py
  - tests/test_kernel_isolation.py
  - tests/test_kernel_logging.py
  - tests/test_kernel_migrate.py
  - tests/test_kernel_update_contract.py
  - tests/test_kernel_version_nudge.py
  - tests/test_kimi_code_adapter.py
  - tests/test_knowledge.py
  - tests/test_labt_validation.py
  - tests/test_large_result_no_notification.py
  - tests/test_large_result_rescan.py
  - tests/test_layers_avatar.py
  - tests/test_layers_bash.py
  - tests/test_layers_email.py
  - tests/test_layers_file.py
  - tests/test_licc_notification_contract_doc.py
  - tests/test_lifecycle_clock.py
  - tests/test_lifecycle_daemon_shutdown.py
  - tests/test_lifecycle_stop_wake.py
  - tests/test_lingtai_doctor.py
  - tests/test_lingtai_facade.py
  - tests/test_llm_adapter_timeouts.py
  - tests/test_llm_adapters_manual.py
  - tests/test_llm_identity_headers.py
  - tests/test_llm_service.py
  - tests/test_llm_service_adapter_cache.py
  - tests/test_llm_utils.py
  - tests/test_local_command_core.py
  - tests/test_logging_setup.py
  - tests/test_loop_guard.py
  - tests/test_macos_shell_adapter.py
  - tests/test_mail_transport.py
  - tests/test_make_tool_executor.py
  - tests/test_max_rpm_plumbing.py
  - tests/test_mcp_capability.py
  - tests/test_mcp_client_close_kills_subprocess.py
  - tests/test_mcp_closed_resource_restart.py
  - tests/test_mcp_config_helper.py
  - tests/test_mcp_identity_discovery.py
  - tests/test_mcp_inbox.py
  - tests/test_mcp_licc_client.py
  - tests/test_mcp_sdk_v2_contract.py
  - tests/test_mcp_server_scaffold.py
  - tests/test_mcp_skill_manuals.py
  - tests/test_mcp_structured_result.py
  - tests/test_mcp_server_credential_logging.py
  - tests/test_mcp_v2_adapter_metadata.py
  - tests/test_messaging_notification_format.py
  - tests/test_meta_block.py
  - tests/test_mimo_adapter.py
  - tests/test_mimo_responses_compaction.py
  - tests/test_molt_notification_persistence.py
  - tests/test_molt_task_persistence.py
  - tests/test_network.py
  - tests/test_notification_attention_cap.py
  - tests/test_notification_cap_doc_parity.py
  - tests/test_notification_delay_alarm.py
  - tests/test_notification_persistent_cap.py
  - tests/test_notification_schema_wire_scrub.py
  - tests/test_notification_settings.py
  - tests/test_notification_store.py
  - tests/test_notification_sync.py
  - tests/test_notification_tool.py
  - tests/test_nudge_inline_cap.py
  - tests/test_nudge_policy.py
  - tests/test_nudge_prompts.py
  - tests/test_openai_compact_threshold.py
  - tests/test_openai_overflow_recovery.py
  - tests/test_openai_prompt_cache_key.py
  - tests/test_openai_responses_streaming.py
  - tests/test_outbound_file_containment.py
  - tests/test_output_hygiene.py
  - tests/test_override_intrinsic.py
  - tests/test_pad.py
  - tests/test_pad_lingtai_split.py
  - tests/test_plugin_tool.py
  - tests/test_perform_refresh_handshake.py
  - tests/test_portable_adapter_encoding.py
  - tests/test_post_molt_notification.py
  - tests/test_powershell_decode_windows_output.py
  - tests/test_powershell_extractor_policy.py
  - tests/test_preset_auto_fallback.py
  - tests/test_preset_connectivity.py
  - tests/test_preset_context_guard.py
  - tests/test_preset_materialization.py
  - tests/test_preset_swap_e2e.py
  - tests/test_presets.py
  - tests/test_project_creation.py
  - tests/test_process_identity.py
  - tests/test_process_match.py
  - tests/test_process_scan.py
  - tests/test_prompt.py
  - tests/test_prompt_catalog.py
  - tests/test_prompt_section_definitions.py
  - tests/test_psyche_family.py
  - tests/test_publish_release_assets.py
  - tests/test_python_support_matrix.py
  - tests/test_read_continuation.py
  - tests/test_refresh_watcher_process.py
  - tests/test_refresh_watcher_windows.py
  - tests/test_release_manifest.py
  - tests/test_repeated_tool_error_continue.py
  - tests/test_resolved_manifest_artifact.py
  - tests/test_responses_converter.py
  - tests/test_retention_report.py
  - tests/test_retroactive_history_compaction.py
  - tests/test_runtime_block_turn_integration.py
  - tests/test_runtime_guidance_globals.py
  - tests/test_runtime_identity.py
  - tests/test_secondary_schema.py
  - tests/test_sent_message_tracker.py
  - tests/test_services_file_io.py
  - tests/test_services_integration.py
  - tests/test_services_logging.py
  - tests/test_services_mail.py
  - tests/test_session.py
  - tests/test_session_journal_gate.py
  - tests/test_shell_kind_classifier.py
  - tests/test_shell_kind_spawn_args.py
  - tests/test_shell_pr1_contract.py
  - tests/test_shell_sandbox_containment.py
  - tests/test_shell_stdin_bootstrap.py
  - tests/test_shell_tool_family_migration.py
  - tests/test_shell_windows_native.py
  - tests/test_shell_windows_state_lock_args.py
  - tests/test_signpost_tool_descriptions.py
  - tests/test_silence_kill.py
  - tests/test_site_schema_quirks.py
  - tests/test_skills.py
  - tests/test_manual_cleanup_routes.py
  - tests/test_snapshot.py
  - tests/test_soul.py
  - tests/test_soul_consultation.py
  - tests/test_soul_settings.py
  - tests/test_source_drift.py
  - tests/test_status_snapshot.py
  - tests/test_streaming.py
  - tests/test_sync_gitee_mirror.py
  - tests/test_system.py
  - tests/test_system_dismiss.py
  - tests/test_system_notifications.py
  - tests/test_system_summarize.py
  - tests/test_task_card_controller.py
  - tests/test_task_card_event_projection_shared.py
  - tests/test_task_card_locale.py
  - tests/test_task_card_proactivity.py
  - tests/test_task_card_resident_shared.py
  - tests/test_taskcard_resident_meta.py
  - tests/test_tc_inbox.py
  - tests/test_tc_inbox_mid_turn_drain.py
  - tests/test_tc_inbox_remove_by_notif_id.py
  - tests/test_tc_wake_chat_not_ready.py
  - tests/test_tc_wake_orphan_heal.py
  - tests/test_telegram_account_last_message_id.py
  - tests/test_telegram_inbound_media_download_failure.py
  - tests/test_telegram_lossless_envelope.py
  - tests/test_telegram_notification_read_state.py
  - tests/test_telegram_rate_limit.py
  - tests/test_telegram_reaction_states.py
  - tests/test_telegram_rich_formatting.py
  - tests/test_telegram_send_media_contract.py
  - tests/test_telegram_slash_commands.py
  - tests/test_telegram_task_card.py
  - tests/test_telegram_task_card_blockers.py
  - tests/test_telegram_task_card_event_tail.py
  - tests/test_telegram_task_card_in_place.py
  - tests/test_telegram_task_card_last_message.py
  - tests/test_telegram_task_card_programmable.py
  - tests/test_telegram_task_card_result_hook.py
  - tests/test_telegram_task_card_rows.py
  - tests/test_telegram_task_card_runtime_drift.py
  - tests/test_telegram_task_card_singleton.py
  - tests/test_telegram_task_card_state.py
  - tests/test_telegram_task_card_timestamp.py
  - tests/test_telegram_task_card_toggle.py
  - tests/test_telegram_task_card_transport.py
  - tests/test_telegram_terra_repairs.py
  - tests/test_telegram_toolfamily_ltpv2.py
  - tests/test_telegram_typing_ttl.py
  - tests/test_telegram_unread_count.py
  - tests/test_three_agent_email.py
  - tests/test_time_awareness_email.py
  - tests/test_time_awareness_mail.py
  - tests/test_time_awareness_status.py
  - tests/test_time_veil.py
  - tests/test_timely_transient_serialization.py
  - tests/test_token_ledger.py
  - tests/test_tool_dispatch.py
  - tests/test_risky_action_gate.py
  - tests/test_tool_executor.py
  - tests/test_tool_family_avatar_migration.py
  - tests/test_tool_family_context_migration.py
  - tests/test_tool_family_daemon_migration.py
  - tests/test_tool_family_email_migration.py
  - tests/test_tool_family_email_wire_parity.py
  - tests/test_tool_family_generic.py
  - tests/test_tool_family_generic_summarize_executor.py
  - tests/test_tool_family_knowledge_migration_parity.py
  - tests/test_tool_family_manual_contract.py
  - tests/test_tool_family_mcp_migration_parity.py
  - tests/test_tool_family_soul_migration.py
  - tests/test_tool_family_system_migration.py
  - tests/test_tool_family_vision_migration.py
  - tests/test_tool_family_wire_parity.py
  - tests/test_tool_glossary.py
  - tests/test_tool_meta_comment_overflow.py
  - tests/test_tool_meta_reconstruction.py
  - tests/test_tool_plugin_declaration.py
  - tests/test_tool_result_recovery.py
  - tests/test_tool_result_restore_after_continuation_failure.py
  - tests/test_tool_result_spill.py
  - tests/test_tool_result_summary.py
  - tests/test_tool_timing.py
  - tests/test_tools_package_data.py
  - tests/test_tools_package_move.py
  - tests/test_trace_redaction.py
  - tests/test_turn_boundary_housekeeping.py
  - tests/test_two_axis_meta_contract.py
  - tests/test_unified_web_capability.py
  - tests/test_utf8_write_text.py
  - tests/test_validate_skill.py
  - tests/test_venv_resolve.py
  - tests/test_vision_capability.py
  - tests/test_vision_services.py
  - tests/test_web_canonical_provider_routing.py
  - tests/test_web_composition_port.py
  - tests/test_web_official_plugin.py
  - tests/test_web_output_spill.py
  - tests/test_web_search_capability.py
  - tests/test_wechat_config_resolution.py
  - tests/test_wechat_inbound_replay.py
  - tests/test_wechat_media_validation.py
  - tests/test_wechat_media_warning_integration.py
  - tests/test_wechat_notification_metadata.py
  - tests/test_wechat_reply_read_state.py
  - tests/test_wechat_settings.py
  - tests/test_wechat_toolfamily_ltpv2.py
  - tests/test_whatsapp_inbound_replay.py
  - tests/test_whatsapp_notification_metadata.py
  - tests/test_whatsapp_personal_bridge.py
  - tests/test_whatsapp_settings.py
  - tests/test_whatsapp_toolfamily_ltpv2.py
  - tests/test_wheel_platlib_layout.py
  - tests/test_wheel_sidecar_smoke.py
  - tests/test_wheels_workflow_publish_gating.py
  - tests/test_win32_job.py
  - tests/test_windows_cmd_shim.py
  - tests/test_windows_import_graph.py
  - tests/test_windows_metachar_scanner.py
  - tests/test_windows_regression.py
  - tests/test_windows_taskkill_fallback.py
  - tests/test_wire_api.py
  - tests/test_wire_orphan_pairing.py
  - tests/test_wire_tool_description.py
  - tests/test_workdir.py
  - tests/test_workdir_lease.py
  - tests/test_workdir_lease_posix_only.py
  - tests/test_zhipu_merge_consecutive.py
  - tests/unit/__init__.py
  - tests/unit/auth/__init__.py
  - tests/unit/auth/test_codex_auth.py
  - tests/_tool_plugin_helpers.py
  - tests/test_cli_liveness.py
  - tests/test_daemon_notification_channel.py
  - tests/test_daemon_task_files.py
  - tests/test_driver_authority_adapter.py
  - tests/test_external_attach_diagnostic.py
  - tests/test_feishu_settings.py
  - tests/test_platform_workflow_release_gating.py
  - tests/test_system_runtime_policy.py
maintenance: |
  Keep related_files repo-relative, duplicate-free, and linked to real files.
  This is a navigation-only inventory anatomy: tests/ is a validation surface,
  not a governed component, so tests/CONTRACT.md deliberately stays outside the
  root contract graph and this file is NOT its governed twin — read that file
  for the testing methodology and do not restate it here. Every tracked file
  under tests/ must appear above, so adding or deleting a test file updates
  this list in the same change; test_architecture_documents.py enforces that.
  Follow the root Anatomy/Contract pairing rule, report mismatches, and do not duplicate or auto-fix the rule here.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# tests/

The kernel's single pytest suite and the repository's conformance evidence: a
flat top-level package of ~380 `test_*.py` modules, a small set of shared
`_`-prefixed helper modules and fake binaries they build on, and two focused
sub-packages. It is a **validation surface, not an architectural component** —
it exposes no Port, implements no promise, and nothing in `src/` imports it.
This anatomy therefore exists to make the file inventory navigable and
complete, not to pair with a governed contract.

## Components

- `CONTRACT.md` — the **Test Methodology Charter**: the test-layer taxonomy
  (unit / integration-lifecycle / contract-conformance), isolation rules, and
  the "a test that passes for the wrong reason is worse than no test" standard.
  It is explicitly *not* a root-governed component contract and is deliberately
  absent from the root contract's `related_files`; this anatomy is its
  navigation neighbour, not its governed twin.
- `conftest.py` — the only shared fixture module. It exposes the
  `make_agent_dir` factory fixture and the autouse hermeticity fixtures that
  stop ambient operator environment (for example
  `LINGTAI_CACHE_MISS_BUDGET`) from leaking into assertions.
- `_*.py` helper modules (20) — the suite's own test infrastructure, imported
  rather than collected. Three families:
  - **Builders/fixtures:** `_agent_dir_helpers.py`, `_agent_presence_helpers.py`,
    `_daemon_helpers.py`, `_lifecycle_clock_helpers.py`, `_molt_helpers.py`,
    `_notification_helpers.py`, `_notification_store_helpers.py`,
    `_migration_workspace_helpers.py`, `_refresh_watcher_helpers.py`,
    `_service_helpers.py`, `_snapshot_helpers.py`, `_workdir_lease_helpers.py`,
    `_chat_completion_helpers.py`.
  - **Test doubles:** `_fake_llm_adapter.py`, `_fake_codex_cli.py`,
    `_fake_codex_app_server.py`, `_fake_resume_cli.py`.
  - **Detached-process parents:** `_detached_cli_parent.py` and
    `_manager_detached_parent.py`, executed as real child processes by the
    daemon and lifecycle suites.
- `codex` and `opencode` — executable `/bin/sh` shims on the test PATH. Each
  execs `_fake_resume_cli.py` under `${PYTHON:-python}`; `opencode` additionally
  exports `FAKE_DAEMON_CLI=opencode` so one fake CLI serves both daemon
  backends. They are tracked, mode-`+x`, extension-less files — not scripts to
  run by hand.
- `integration_test_*.py` — `integration_test_claude_code.py` and
  `integration_test_kimi_code.py`, named off the `test_*` collection pattern so
  the default run does not drive a real local CLI.
- `contracts/llm_conversation_input/` — the contract-conformance sub-package for
  provider conversation input. `tests/contracts/llm_conversation_input/regimes.py` holds the executable
  session-return matrix (`REGISTRY_EDGES`) that builds every registered
  provider through the real `LLMService`, and `tests/contracts/llm_conversation_input/test_regime_inventory.py` asserts
  that matrix equals the live registry key set, so adding a provider or
  rebinding a factory fails here rather than silently.
- `unit/` and `unit/auth/` — the one explicitly-layered unit sub-package,
  currently holding `tests/unit/auth/test_codex_auth.py`.
- `test_*.py` (~380 modules at the top level) — the bulk of the suite, named
  after the surface each covers rather than organized into directories.
  `test_project_creation.py` is focused evidence for one fresh Project seed,
  existing-target refusal, reader acceptance, and no Agent start. `test_architecture_documents.py`,
  `test_docs_governance.py`,
  `test_anatomy_drift_checker.py`, and `test_source_drift.py` are the
  self-validation group that keeps this repository's own documents honest.

## Connections

`pyproject.toml` configures collection; `AGENTS.md` and
[`dev-guide-skill/SKILL.md`](../dev-guide-skill/SKILL.md) own the
narrow-to-broad validation workflow that decides which subset an agent runs
before calling a change done. Individual component anatomies and contracts
across `src/` cite specific test modules as their conformance evidence, so the
edges into this directory are many and one-way: tests import from `lingtai`,
never the reverse.

`test_architecture_documents.py` is the enforcement point for the anatomy graph
itself — it validates frontmatter shape, reciprocal governed pairs, and that
every tracked file in the repository is reachable from the root anatomy through
`related_files`. `scripts/check_docs_governance.py` and
`src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/scripts/check_anatomy_drift.py`
are the two companion checkers, covered by `test_docs_governance.py` and
`test_anatomy_drift_checker.py`.

## Composition

- **Parent:** the repository root ([`ANATOMY.md`](../ANATOMY.md)).
- **Neighbour:** [`CONTRACT.md`](CONTRACT.md) in this directory — the
  methodology charter, deliberately outside the governed contract graph.
- **No child anatomies.** `contracts/` and `unit/` are collection sub-packages,
  not architectural layers; they are mapped above rather than given their own
  files.

## State

The suite writes no durable repository state. Every test owns its own state
under `tmp_path`, per the isolation principle in `CONTRACT.md`; the detached
parents and fake CLIs spawn real processes that must be reaped within the test
that started them. Ambient environment is neutralized by `conftest.py`'s
autouse fixtures rather than by per-test cleanup.

## Notes

- The flat top-level layout is deliberate. A test's name is its index, so
  reaching for it by `git ls-files 'tests/test_*<topic>*'` is the intended
  lookup, and new tests join the flat namespace rather than growing a directory
  tree.
- `codex` and `opencode` are the only tracked extension-less executables in the
  repository. A tooling change that filters files by suffix will silently drop
  them.
- `integration_test_*.py` files are invisible to a default `pytest` run by
  design; they require a real `claude` or `kimi` CLI and are invoked explicitly.
