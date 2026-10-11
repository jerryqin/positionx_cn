# PositionX-CN Operations — P2 Draft

## Safety invariants
- Broker holdings are the account truth source.
- Manual approval is mandatory for real trading.
- Preview/readiness PASS is never authorization to submit an order.
- Fail closed on stale/missing/unreconciled state.
- Scheduled success should be validated, not blindly duplicated.

## Verified command surfaces
The entries below are derived statically from tracked files. P2A does not execute operational scripts.

- `deep_forward` → `python scripts/run_deep_oversold_csi500_forward.py`; exact CLI arguments were not statically proven by P2A.
- `ret120_forward` → `python scripts/run_ret120_csi500_forward.py <command>`; statically detected choices: `bootstrap`, `check`, `run`, `status`
- `portfolio_shadow` → `python scripts/run_strategy_portfolio_shadow_v01.py <command>`; statically detected choices: `bootstrap`, `check`, `snapshot`, `status`
- `optimus_steady` → `python scripts/run_optimus_steady_state_v074.py <command>`; statically detected choices: `check`, `status`
- `optimus_scheduler` → `python scripts/run_optimus_current22_scheduled_v074.py <subcommand>`; statically detected subcommands: `ack-attention`, `attention`, `run`, `status`
- `optimus_acceptance` → `python scripts/run_optimus_v045_first_scheduled_acceptance.py <command>`; statically detected choices: `check`, `inspect`
- `g4_reservation` → `python scripts/run_g4_forward_reservation_v02.py <command>`; statically detected choices: `CANARY`, `EXPANDED`, `bootstrap`, `inspect`, `reserve`
- `g4_open_confirmation` → `python scripts/run_g4_open_confirmation_v03.py <command>`; statically detected choices: `check`, `confirm`
- `g4_preview` → `python scripts/run_g4_isolated_preview_v04.py <command>`; statically detected choices: `check`, `preview`
- `g4_manual_approval` → `python scripts/run_g4_manual_approval_v05.py <command>`; statically detected choices: `approve`, `check`, `prepare`
- `daily_data_update` → `python scripts/update_a_share_daily_v1.py <subcommand>`; statically detected subcommands: `check`, `run`, `self-test`, `status`
- `main` → `python main.py <command>`; statically detected choices: `BUY`, `SELL`, `check`, `daemon`, `dry-run`, `exact`, `execute`, `gui`, `init-db`, `kill-off`, `kill-on`, `plan`, `reconcile`

## Environment contract
- Referenced environment-variable names: `IWAOS_DATA_ROOT`, `POSITIONX_RET120_DEEP_P3`, `POSITIONX_RET120_FAMILY_P1`, `POSITIONX_SEC_CONTACT_EMAIL`, `POSITIONX_STRATEGY2_OUT`, `PYTHONIOENCODING`, `PYTHONUTF8`, `USERDOMAIN`, `USERNAME`.
- Values are intentionally not recorded in Git documentation.

## Daily-runbook hardening requirement
P2B should convert the verified command surfaces into a concise daily checklist, and explicitly mark commands that are read-only, mutating research state, scheduling tasks, previewing orders, or requiring manual approval.
