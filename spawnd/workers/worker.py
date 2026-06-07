"""Deployed worker execution loop."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from spawnd.artifacts.store import ArtifactStore, store_redacted_text_artifact
from spawnd.state.submission import claim_next_agent, enqueue_newly_ready_agents
from spawnd.coordination.redis import CoordinationPlane
from spawnd.state.repository import ClaimedAgent, DeployedRepository
from spawnd.observability.telemetry import TelemetryRecorder
from spawnd.gitops.worktrees import GitError, create_worktree, run_git, run_worktree_setup, setup_worktree_with_deps
from spawnd.io.plan_builder import load_shared_context
from spawnd.models.specs import AgentSpec, PlanSpec
from spawnd.runtime.agent_config import resolve_agent_plan_config
from spawnd.runtime.agent_run import AgentConfig
from spawnd.runtime.executor import run_manager, run_worker, run_worker_mock
from spawnd.runtime.observer import PostgresRuntimeObserver


@dataclass(frozen=True)
class WorkerRunResult:
    """Result of one deployed worker poll."""

    claimed: bool
    run_id: str | None = None
    agent: str | None = None
    status: str | None = None


@dataclass(frozen=True)
class RunSource:
    """Resolved local source repository used to create a worker scratch worktree."""

    repo_path: Path
    base_ref: str | None
    fetch: bool


class DeployedWorker:
    """Execute claimed deployed agents."""

    def __init__(
        self,
        *,
        repository: DeployedRepository,
        coordinator: CoordinationPlane,
        artifacts: ArtifactStore,
        telemetry: TelemetryRecorder,
        worker_id: str,
        source_path: Path | None = None,
        lease_seconds: int = 300,
        use_mock: bool = False,
    ) -> None:
        self.repository = repository
        self.coordinator = coordinator
        self.artifacts = artifacts
        self.telemetry = telemetry
        self.worker_id = worker_id
        self.source_path = (source_path or Path.cwd()).resolve()
        self.lease_seconds = lease_seconds
        self.use_mock = use_mock
        self.capture_raw_artifacts = False

    async def run_once(self, *, block_ms: int = 1000) -> WorkerRunResult:
        """Claim and execute at most one queued agent."""

        self.heartbeat()
        claimed_pair = claim_next_agent(
            repository=self.repository,
            coordinator=self.coordinator,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            block_ms=block_ms,
        )
        if claimed_pair is None:
            return WorkerRunResult(claimed=False)
        _, claimed = claimed_pair
        status = await self._execute_claimed(claimed)
        return WorkerRunResult(claimed=True, run_id=claimed.run_id, agent=claimed.name, status=status)

    async def run_poll(self, *, idle_sleep_seconds: float = 1.0, block_ms: int = 1000) -> None:
        """Continuously claim and execute agents."""

        while True:
            result = await self.run_once(block_ms=block_ms)
            if not result.claimed:
                await asyncio.sleep(idle_sleep_seconds)

    def heartbeat(self) -> None:
        self.repository.record_worker_heartbeat(
            self.worker_id,
            hostname=socket.gethostname(),
            capacity={'pid': os.getpid()},
        )
        self.coordinator.heartbeat(self.worker_id)

    async def _execute_claimed(self, claimed: ClaimedAgent) -> str:
        run = self.repository.get_run(claimed.run_id)
        agent_row = self.repository.get_agent(claimed.run_id, claimed.name)
        if run is None or agent_row is None:
            return 'missing'
        plan = PlanSpec(**run['spec'])
        self.capture_raw_artifacts = bool(
            plan.orchestration
            and plan.orchestration.artifacts
            and plan.orchestration.artifacts.capture_raw
        )
        agent = _find_agent(plan, claimed.name)
        if agent is None:
            error = f'Agent {claimed.name} is missing from run spec'
            error_id = self.repository.record_runtime_error(
                run_id=claimed.run_id,
                agent=claimed.name,
                attempt_id=claimed.attempt_id,
                source='spawnd',
                message=error,
            )
            self.repository.fail_agent(claimed.run_id, claimed.name, error, attempt_id=claimed.attempt_id, error_id=error_id)
            return 'failed'
        provider = _provider_for_runtime(claimed.runtime)
        try:
            source = self._resolve_run_source(run, plan)
            self.repository.append_event(
                claimed.run_id,
                claimed.name,
                'worktree_source_resolved',
                {'source_repo': str(source.repo_path), 'base_ref': source.base_ref, 'fetch': source.fetch},
            )
        except Exception as exc:
            error = str(exc)
            artifact_id = self._store_text_artifact(
                claimed.run_id,
                claimed.name,
                'source-error',
                error,
                attempt_id=claimed.attempt_id,
            )
            error_id = self.repository.record_runtime_error(
                run_id=claimed.run_id,
                agent=claimed.name,
                attempt_id=claimed.attempt_id,
                source='worktree_source',
                message=error,
                details_artifact_id=artifact_id,
            )
            self.repository.fail_agent(claimed.run_id, claimed.name, error, attempt_id=claimed.attempt_id, error_id=error_id)
            return 'failed'
        with _pushd(source.repo_path):
            try:
                worktree = self._prepare_worktree(plan, agent, claimed, source)
            except Exception as exc:
                error = str(exc)
                artifact_id = self._store_text_artifact(
                    claimed.run_id,
                    claimed.name,
                    'worktree-error',
                    error,
                    attempt_id=claimed.attempt_id,
                )
                error_id = self.repository.record_runtime_error(
                    run_id=claimed.run_id,
                    agent=claimed.name,
                    attempt_id=claimed.attempt_id,
                    source='worktree_create',
                    message=error,
                    details_artifact_id=artifact_id,
                )
                self.repository.fail_agent(claimed.run_id, claimed.name, error, attempt_id=claimed.attempt_id, error_id=error_id)
                return 'failed'
            hydrated = resolve_agent_plan_config(agent, plan.defaults)
            session_id = self.repository.record_runtime_session(
                attempt_id=claimed.attempt_id,
                run_id=claimed.run_id,
                agent=claimed.name,
                provider=provider,
                runtime=f'{claimed.runtime}_sdk' if claimed.runtime != 'codex' else 'codex',
                model=hydrated.model,
                cwd_locator=str(worktree),
                metadata={'attempt': claimed.attempt_number},
            )
            if self.telemetry.initialization_error:
                self.repository.record_runtime_error(
                    run_id=claimed.run_id,
                    agent=claimed.name,
                    attempt_id=claimed.attempt_id,
                    session_id=session_id,
                    source='otel',
                    message=self.telemetry.initialization_error,
                    retryable=False,
                )
            setup_ok = self._run_setup_if_needed(plan, agent, claimed, worktree, session_id, source.repo_path)
            if not setup_ok:
                return 'failed'
            runtime_status, runtime_result = await self._run_runtime(plan, agent, claimed, worktree, hydrated, session_id, provider)
            if runtime_status != 'completed':
                return runtime_status
            check_status = self._run_check(claimed, hydrated.check_command, worktree, session_id)
            if check_status != 'completed':
                return check_status
            self._record_git_provenance(claimed, worktree, source)
            input_tokens = int(runtime_result.get('input_tokens') or 0)
            output_tokens = int(runtime_result.get('output_tokens') or 0)
            cost = float(runtime_result.get('cost') or 0.0)
            self.repository.record_token_usage(
                run_id=claimed.run_id,
                agent=claimed.name,
                attempt_id=claimed.attempt_id,
                session_id=session_id,
                provider=provider,
                model=hydrated.model,
                scope='result_total',
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            self.repository.record_cost_usage(
                run_id=claimed.run_id,
                agent=claimed.name,
                attempt_id=claimed.attempt_id,
                session_id=session_id,
                provider=provider,
                model=hydrated.model,
                amount_usd=cost,
                source=str(runtime_result.get('cost_source') or agent_row.get('cost_source') or 'unknown'),
            )
            ready = self.repository.complete_agent(
                claimed.run_id,
                claimed.name,
                cost_usd=cost,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                attempt_id=claimed.attempt_id,
            )
            for name in ready:
                self._enqueue_agent(claimed.run_id, name)
            enqueue_newly_ready_agents(claimed.run_id, repository=self.repository, coordinator=self.coordinator)
            return 'completed'

    def _resolve_run_source(self, run: dict, plan: PlanSpec) -> RunSource:
        """Resolve the submitted run source into a local git repository root."""

        configured = plan.orchestration.worktree_source if plan.orchestration else None
        source_repo = run.get('source_repo') or str(self.source_path)
        candidate = Path(str(source_repo)).expanduser()
        if not candidate.exists():
            raise GitError(f'Run source repository does not exist: {candidate}')
        result = run_git(['rev-parse', '--show-toplevel'], cwd=candidate, check=False)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or 'not a git repository'
            raise GitError(f'Run source repository is not a git repository: {candidate}: {detail}')
        repo_path = Path(result.stdout.strip()).resolve()
        configured_base = configured.base_ref if configured and configured.base_ref else None
        base_ref = configured_base or run.get('source_ref')
        return RunSource(
            repo_path=repo_path,
            base_ref=str(base_ref) if base_ref else None,
            fetch=configured.fetch if configured else False,
        )

    def _prepare_worktree(self, plan: PlanSpec, agent: AgentSpec, claimed: ClaimedAgent, source: RunSource) -> Path:
        with self.telemetry.span('spawnd.worktree.create', run_id=claimed.run_id, agent=claimed.name):
            worktree = create_worktree(
                claimed.run_id,
                claimed.name,
                repo_path=source.repo_path,
                base_ref=source.base_ref,
                fetch=source.fetch,
            )
        if agent.depends_on:
            dep_context = plan.orchestration.dependency_context if plan.orchestration else None
            setup_worktree_with_deps(
                claimed.run_id,
                claimed.name,
                list(agent.depends_on),
                worktree,
                mode=dep_context.mode if dep_context else 'full',
                include_paths=dep_context.include_paths if dep_context else None,
                exclude_paths=dep_context.exclude_paths if dep_context else None,
            )
        self.repository.update_agent_worktree(
            claimed.run_id,
            claimed.name,
            worktree_locator=str(worktree),
            branch=f'spawnd/{claimed.run_id}/{claimed.name}',
        )
        return worktree

    def _run_setup_if_needed(self, plan: PlanSpec, agent: AgentSpec, claimed: ClaimedAgent, worktree: Path, session_id: str, source_path: Path) -> bool:
        setup = plan.orchestration.worktree_setup if plan.orchestration else None
        if setup is None:
            return True
        invocation_id = self.repository.start_runtime_invocation(
            attempt_id=claimed.attempt_id,
            run_id=claimed.run_id,
            agent=claimed.name,
            session_id=session_id,
            kind='setup',
        )
        with self.telemetry.span('spawnd.worktree.setup', run_id=claimed.run_id, agent=claimed.name, attributes={'command': setup.command}):
            try:
                result = run_worktree_setup(
                    worktree,
                    source_path,
                    setup.command,
                    env={**agent.env, **setup.env},
                    timeout_seconds=setup.timeout_seconds,
                )
            except Exception as exc:
                artifact_id = self._store_text_artifact(
                    claimed.run_id,
                    claimed.name,
                    'setup-error',
                    str(exc),
                    attempt_id=claimed.attempt_id,
                    session_id=session_id,
                    invocation_id=invocation_id,
                )
                error_id = self.repository.record_runtime_error(
                    run_id=claimed.run_id,
                    agent=claimed.name,
                    attempt_id=claimed.attempt_id,
                    session_id=session_id,
                    invocation_id=invocation_id,
                    source='worktree_setup',
                    message=str(exc),
                    details_artifact_id=artifact_id,
                )
                self.repository.finish_runtime_invocation(invocation_id, status='failed', error_id=error_id)
                self.repository.fail_agent(claimed.run_id, claimed.name, str(exc), attempt_id=claimed.attempt_id, error_id=error_id)
                return False
        artifact_id = self._store_text_artifact(
            claimed.run_id,
            claimed.name,
            'setup-output',
            f'[stdout]\n{result.stdout}\n[stderr]\n{result.stderr}',
            attempt_id=claimed.attempt_id,
            session_id=session_id,
            invocation_id=invocation_id,
        )
        self.repository.finish_runtime_invocation(invocation_id, status='completed', final_message_artifact_id=artifact_id)
        return True

    async def _run_runtime(
        self,
        plan: PlanSpec,
        agent: AgentSpec,
        claimed: ClaimedAgent,
        worktree: Path,
        hydrated: object,
        session_id: str,
        provider: str,
    ) -> tuple[str, dict]:
        invocation_id = self.repository.start_runtime_invocation(
            attempt_id=claimed.attempt_id,
            run_id=claimed.run_id,
            agent=claimed.name,
            session_id=session_id,
            kind='runtime',
        )
        shared_context = load_shared_context(plan.shared_context) if plan.shared_context else ''
        observer = PostgresRuntimeObserver(
            repository=self.repository,
            run_id=claimed.run_id,
            agent=claimed.name,
            attempt_id=claimed.attempt_id,
            session_id=session_id,
            invocation_id=invocation_id,
            provider=provider,
            runtime=claimed.runtime,
        )
        config = AgentConfig(
            name=claimed.name,
            run_id=claimed.run_id,
            prompt=hydrated.prompt,
            worktree=worktree,
            check_command=hydrated.check_command or 'true',
            model=hydrated.model,
            max_iterations=hydrated.max_iterations,
            max_cost_usd=hydrated.max_cost_usd,
            env=agent.env or None,
            shared_context=shared_context,
            runtime=hydrated.runtime,
            observer=observer,
        )
        try:
            with self.telemetry.span('spawnd.runtime.invocation', run_id=claimed.run_id, agent=claimed.name, attributes={'runtime': claimed.runtime}):
                if self.use_mock:
                    result = await self._with_lease_renewal(claimed, run_worker_mock(config))
                elif claimed.type == 'manager':
                    result = await self._with_lease_renewal(claimed, run_manager(config))
                else:
                    result = await self._with_lease_renewal(claimed, run_worker(config))
        except asyncio.CancelledError:
            observer.flush()
            error = 'Run cancelled'
            artifact_id = self._store_text_artifact(
                claimed.run_id,
                claimed.name,
                'runtime-error',
                error,
                attempt_id=claimed.attempt_id,
                session_id=session_id,
                invocation_id=invocation_id,
            )
            error_id = self.repository.record_runtime_error(
                run_id=claimed.run_id,
                agent=claimed.name,
                attempt_id=claimed.attempt_id,
                session_id=session_id,
                invocation_id=invocation_id,
                source='worker_cancel',
                message=error,
                details_artifact_id=artifact_id,
            )
            self.repository.finish_runtime_invocation(invocation_id, status='cancelled', error_id=error_id)
            self.repository.cancel_agent(claimed.run_id, claimed.name, error)
            return ('cancelled', {'success': False, 'status': 'cancelled', 'error': error})
        except Exception as exc:
            observer.flush()
            artifact_id = self._store_text_artifact(
                claimed.run_id,
                claimed.name,
                'runtime-error',
                str(exc),
                attempt_id=claimed.attempt_id,
                session_id=session_id,
                invocation_id=invocation_id,
            )
            error_id = self.repository.record_runtime_error(
                run_id=claimed.run_id,
                agent=claimed.name,
                attempt_id=claimed.attempt_id,
                session_id=session_id,
                invocation_id=invocation_id,
                source=f'{claimed.runtime}_runtime',
                message=str(exc),
                details_artifact_id=artifact_id,
            )
            self.repository.finish_runtime_invocation(invocation_id, status='failed', error_id=error_id)
            self.repository.fail_agent(claimed.run_id, claimed.name, str(exc), attempt_id=claimed.attempt_id, error_id=error_id)
            return ('failed', {'success': False, 'error': str(exc)})
        observer.flush()
        status = str(result.get('status') or 'failed')
        success = bool(result.get('success')) and status == 'completed'
        runtime_text = str(result.get('stdout') or '') + str(result.get('stderr') or '')
        if not runtime_text:
            runtime_text = observer.final_text or str(result.get('final_message') or result.get('final_output') or '')
        artifact_id = self._store_text_artifact(
            claimed.run_id,
            claimed.name,
            'runtime-output',
            runtime_text,
            attempt_id=claimed.attempt_id,
            session_id=session_id,
            invocation_id=invocation_id,
        )
        final_text = str(result.get('final_message') or result.get('final_output') or observer.final_text or '')
        final_artifact_id = (
            self._store_text_artifact(
                claimed.run_id,
                claimed.name,
                'final-message',
                final_text,
                attempt_id=claimed.attempt_id,
                session_id=session_id,
                invocation_id=invocation_id,
            )
            if final_text
            else artifact_id
        )
        if result.get('vendor_session_id'):
            self.repository.append_event(
                claimed.run_id,
                claimed.name,
                'vendor_session',
                {'vendor_session_id': result.get('vendor_session_id'), 'provider': provider},
            )
        self.repository.finish_runtime_invocation(
            invocation_id,
            status='completed' if success else status,
            final_message_artifact_id=final_artifact_id,
        )
        if not success:
            error = str(result.get('error') or f'Runtime ended with status {status}')
            error_id = self.repository.record_runtime_error(
                run_id=claimed.run_id,
                agent=claimed.name,
                attempt_id=claimed.attempt_id,
                session_id=session_id,
                invocation_id=invocation_id,
                source=f'{claimed.runtime}_runtime',
                message=error,
                details_artifact_id=artifact_id,
            )
            self.repository.fail_agent(claimed.run_id, claimed.name, error, attempt_id=claimed.attempt_id, error_id=error_id)
            return ('failed', result)
        if observer.input_tokens and not result.get('input_tokens'):
            result['input_tokens'] = observer.input_tokens
        if observer.output_tokens and not result.get('output_tokens'):
            result['output_tokens'] = observer.output_tokens
        if observer.cost_usd and not result.get('cost'):
            result['cost'] = observer.cost_usd
        if observer.cost_source != 'unknown' and not result.get('cost_source'):
            result['cost_source'] = observer.cost_source
        return ('completed', result)

    async def _with_lease_renewal(self, claimed: ClaimedAgent, awaitable) -> dict:
        task = asyncio.create_task(awaitable)
        renew_task = asyncio.create_task(self._renew_lease_until_done(claimed, task))
        try:
            return await task
        finally:
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass

    async def _renew_lease_until_done(self, claimed: ClaimedAgent, task: asyncio.Task) -> None:
        interval = max(1.0, min(30.0, self.lease_seconds / 3))
        while not task.done():
            await asyncio.sleep(interval)
            if self.coordinator.is_cancelled(claimed.run_id):
                task.cancel()
                return
            postgres_ok = self.repository.renew_lease(
                claimed.run_id,
                claimed.name,
                worker_id=claimed.worker_id,
                lease_token=claimed.lease_token,
                lease_seconds=self.lease_seconds,
            )
            redis_ok = self.coordinator.renew_lease(
                claimed.run_id,
                claimed.name,
                claimed.lease_token,
                self.lease_seconds,
            )
            if not postgres_ok or not redis_ok:
                task.cancel()
                return

    def _run_check(self, claimed: ClaimedAgent, command: str, worktree: Path, session_id: str) -> str:
        invocation_id = self.repository.start_runtime_invocation(
            attempt_id=claimed.attempt_id,
            run_id=claimed.run_id,
            agent=claimed.name,
            session_id=session_id,
            kind='check',
        )
        started = datetime.now(timezone.utc)
        with self.telemetry.span('spawnd.check', run_id=claimed.run_id, agent=claimed.name, attributes={'command': command}):
            result = subprocess.run(command or 'true', shell=True, cwd=worktree, capture_output=True, text=True)
        completed = datetime.now(timezone.utc)
        duration_ms = max(0, int((completed - started).total_seconds() * 1000))
        artifact_id = self._store_text_artifact(
            claimed.run_id,
            claimed.name,
            'check-output',
            f'[stdout]\n{result.stdout}\n[stderr]\n{result.stderr}',
            attempt_id=claimed.attempt_id,
            session_id=session_id,
            invocation_id=invocation_id,
        )
        self.repository.record_check(
            run_id=claimed.run_id,
            agent=claimed.name,
            command=command or 'true',
            exit_code=result.returncode,
            duration_ms=duration_ms,
            output_artifact_id=artifact_id,
            attempt_id=claimed.attempt_id,
            runtime_invocation_id=invocation_id,
            shell='/bin/sh',
            cwd_locator=str(worktree),
            started_at=started,
            completed_at=completed,
        )
        self.repository.finish_runtime_invocation(
            invocation_id,
            status='completed' if result.returncode == 0 else 'failed',
            exit_code=result.returncode,
            final_message_artifact_id=artifact_id,
        )
        if result.returncode == 0:
            return 'completed'
        error = result.stderr.strip() or result.stdout.strip() or f'check exited {result.returncode}'
        error_id = self.repository.record_runtime_error(
            run_id=claimed.run_id,
            agent=claimed.name,
            attempt_id=claimed.attempt_id,
            session_id=session_id,
            invocation_id=invocation_id,
            source='check',
            message=error,
            details_artifact_id=artifact_id,
        )
        self.repository.fail_agent(claimed.run_id, claimed.name, error, attempt_id=claimed.attempt_id, error_id=error_id)
        return 'failed'

    def _record_git_provenance(self, claimed: ClaimedAgent, worktree: Path, source: RunSource) -> None:
        base_ref = source.base_ref
        with self.telemetry.span('spawnd.git.provenance', run_id=claimed.run_id, agent=claimed.name):
            head = _git_output(['rev-parse', 'HEAD'], worktree)
            branch = _git_output(['branch', '--show-current'], worktree)
            remote = _git_output(['remote', 'get-url', 'origin'], worktree, check=False) or None
            base_sha = _git_output(['rev-parse', str(base_ref)], worktree, check=False) if base_ref else None
            merge_base = _git_output(['merge-base', str(base_ref), 'HEAD'], worktree, check=False) if base_ref else None
            diff_base = merge_base or base_sha
            committed_range = f'{diff_base}..HEAD' if diff_base else None
            if committed_range:
                committed_shortstat = _git_output(['diff', '--shortstat', committed_range], worktree, check=False)
                committed_numstat = _git_output(['diff', '--numstat', committed_range], worktree, check=False)
                committed_patch = _git_output(['diff', committed_range], worktree, check=False)
            else:
                committed_shortstat = ''
                committed_numstat = ''
                committed_patch = ''
            worktree_shortstat = _git_output(['diff', '--shortstat', 'HEAD'], worktree, check=False)
            worktree_numstat = _git_output(['diff', '--numstat', 'HEAD'], worktree, check=False)
            worktree_patch = _git_output(['diff', 'HEAD'], worktree, check=False)
            commit_message = _git_output(['log', '-1', '--pretty=%B'], worktree, check=False)
            pr_url, pr_number = _pull_request_for_branch(branch, worktree) if branch else (None, None)
        patch_parts = [part for part in [committed_patch, worktree_patch] if part]
        patch = '\n'.join(patch_parts)
        patch_artifact_id = (
            self._store_text_artifact(
                claimed.run_id,
                claimed.name,
                'patch',
                patch,
                attempt_id=claimed.attempt_id,
            )
            if patch
            else None
        )
        changed_files_count, insertions_count, deletions_count = _numstat_summary(committed_numstat, worktree_numstat)
        self.repository.record_git_provenance(
            run_id=claimed.run_id,
            agent=claimed.name,
            attempt_id=claimed.attempt_id,
            base_ref=str(base_ref) if base_ref else None,
            remote=remote,
            worktree_locator=str(worktree),
            base_sha=base_sha or None,
            merge_base_sha=merge_base or None,
            head_sha=head or None,
            branch=branch or None,
            commit_sha=head or None,
            pr_url=pr_url,
            pr_number=pr_number,
            patch_artifact_id=patch_artifact_id,
            commit_message=commit_message or None,
            changed_files_count=changed_files_count,
            insertions_count=insertions_count,
            deletions_count=deletions_count,
            diff_stats={
                'base_ref': base_ref,
                'source_repo': str(source.repo_path),
                'range': committed_range,
                'committed_shortstat': committed_shortstat,
                'committed_numstat': committed_numstat,
                'worktree_shortstat': worktree_shortstat,
                'worktree_numstat': worktree_numstat,
                'patch_artifact_id': patch_artifact_id,
            },
        )

    def _store_text_artifact(
        self,
        run_id: str,
        agent: str | None,
        kind: str,
        text: str,
        *,
        attempt_id: str | None = None,
        session_id: str | None = None,
        invocation_id: str | None = None,
    ) -> str:
        with self.telemetry.span('spawnd.artifact.upload', run_id=run_id, agent=agent, attributes={'kind': kind}):
            blob = store_redacted_text_artifact(
                self.artifacts,
                run_id=run_id,
                agent=agent,
                kind=kind,
                text=text,
                capture_raw=self.capture_raw_artifacts,
            )
        return self.repository.record_artifact(
            run_id=run_id,
            agent=agent,
            kind=kind,
            uri=blob.uri,
            sha256=blob.sha256,
            size_bytes=blob.size_bytes,
            redaction_policy=blob.redaction_policy,
            content_type=blob.content_type,
            attempt_id=attempt_id,
            session_id=session_id,
            invocation_id=invocation_id,
        )

    def _enqueue_agent(self, run_id: str, agent: str) -> None:
        _publish_ready_agent(self.repository, self.coordinator, run_id, agent)


def reconcile_ready_agents(repository: DeployedRepository, coordinator: CoordinationPlane) -> list[dict[str, str]]:
    """Recover Redis queue hints from canonical Postgres state."""

    requeued: list[dict[str, str]] = []
    published: set[tuple[str, str]] = set()
    for expired in repository.expire_stale_leases():
        _publish_ready_agent(repository, coordinator, expired['run_id'], expired['agent'])
        requeued.append({'run_id': expired['run_id'], 'agent': expired['agent']})
        published.add((expired['run_id'], expired['agent']))
    for run in repository.list_runs(limit=1000):
        run_id = str(run['run_id'])
        for agent in repository.ready_agents(run_id):
            if (run_id, agent) in published:
                continue
            _publish_ready_agent(repository, coordinator, run_id, agent)
            requeued.append({'run_id': run_id, 'agent': agent})
            published.add((run_id, agent))
    return requeued


def _publish_ready_agent(repository: DeployedRepository, coordinator: CoordinationPlane, run_id: str, agent: str) -> None:
    outbox_id = repository.record_queue_outbox(run_id, agent, 'agent_ready', {'run_id': run_id, 'agent': agent})
    coordinator.enqueue_agent(run_id, agent)
    repository.mark_outbox_published(outbox_id)


def _find_agent(plan: PlanSpec, name: str) -> AgentSpec | None:
    return next((agent for agent in plan.agents if agent.name == name), None)


def _provider_for_runtime(runtime: str) -> str:
    if runtime == 'claude':
        return 'anthropic'
    if runtime in {'codex', 'openai'}:
        return 'openai'
    return 'unknown'


def _git_output(args: list[str], cwd: Path, *, check: bool = True) -> str:
    result = run_git(args, cwd=cwd, check=check)
    return result.stdout.strip()


def _numstat_summary(*values: str) -> tuple[int, int, int]:
    paths: set[str] = set()
    insertions = 0
    deletions = 0
    for value in values:
        for line in value.splitlines():
            parts = line.split('\t')
            if len(parts) < 3:
                continue
            paths.add(parts[2])
            if parts[0].isdigit():
                insertions += int(parts[0])
            if parts[1].isdigit():
                deletions += int(parts[1])
    return (len(paths), insertions, deletions)


def _pull_request_for_branch(branch: str, cwd: Path) -> tuple[str | None, int | None]:
    try:
        result = subprocess.run(
            ['gh', 'pr', 'list', '--head', branch, '--json', 'url,number', '--limit', '1'],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return (None, None)
    if result.returncode != 0:
        return (None, None)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return (None, None)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return (None, None)
    number = data.get('number')
    return (data.get('url'), int(number) if isinstance(number, int) else None)


@contextmanager
def _pushd(path: Path) -> Iterator[None]:
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)
