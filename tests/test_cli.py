"""Tests for CLI module."""
import os

from spawnd.cli import main
from spawnd.storage.db import init_db, insert_agent, insert_plan, update_agent_status

from tests.helpers import insert_test_plan

def test_main_help(runner):
    """Test main CLI help."""
    result = runner.invoke(main, ['--help'])
    assert result.exit_code == 0
    assert 'spawnd.dev' in result.output

def test_run_requires_file_or_prompt(runner):
    """Test run command requires --file or --prompt."""
    result = runner.invoke(main, ['run'])
    assert result.exit_code != 0
    assert 'Either --file or --prompt is required' in result.output

def test_resume_requires_run_id(runner):
    """Test --resume requires --run-id."""
    result = runner.invoke(main, ['run', '--resume'])
    assert result.exit_code != 0
    assert '--resume requires --run-id' in result.output

def test_status_no_runs(runner, temp_spawnd_dir):
    """Test status command with no runs."""
    result = runner.invoke(main, ['status'])
    assert result.exit_code != 0
    assert 'No runs found' in result.output

def test_status_with_run(runner, temp_spawnd_dir):
    """Test status command with existing run."""
    run_id = 'test-run-1'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test-plan", status="pending")
    _ = insert_agent(db, run_id, 'agent1', 'Test prompt')
    _ = db.close()
    result = runner.invoke(main, ['status', run_id])
    assert result.exit_code == 0
    assert run_id in result.output
    assert 'agent1' in result.output

def test_status_json_output(runner, temp_spawnd_dir):
    """Test status command JSON output."""
    run_id = 'test-run-json'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test-plan", status="running")
    _ = insert_agent(db, run_id, 'agent1', 'Test prompt')
    _ = update_agent_status(db, run_id, 'agent1', 'completed')
    _ = db.close()
    result = runner.invoke(main, ['status', run_id, '--json'])
    assert result.exit_code == 0
    assert '"run_id": "test-run-json"' in result.output
    assert '"status": "completed"' in result.output

def test_status_nonexistent_run(runner, temp_spawnd_dir):
    """Test status command with nonexistent run."""
    result = runner.invoke(main, ['status', 'nonexistent'])
    assert result.exit_code != 0
    assert 'Run not found: nonexistent' in result.output

def test_status_without_run_id_uses_latest_valid_run(runner, temp_spawnd_dir):
    """Latest run selection should follow filesystem mtime, not name sorting."""
    older = 'zzz-older'
    db = init_db(older)
    _ = insert_plan(db, older, 'older-plan', 'name: older')
    _ = db.close()
    _ = os.utime(temp_spawnd_dir / '.spawnd' / 'runs' / older, (1, 1))
    newer = 'aaa-newer'
    db = init_db(newer)
    _ = insert_plan(db, newer, 'newer-plan', 'name: newer')
    _ = db.close()
    _ = os.utime(temp_spawnd_dir / '.spawnd' / 'runs' / newer, (2, 2))
    result = runner.invoke(main, ['status'])
    assert result.exit_code == 0
    assert 'Run: aaa-newer' in result.output
    assert 'Plan: newer-plan' in result.output

def test_status_without_run_id_skips_invalid_runs(runner, temp_spawnd_dir):
    """Broken run directories should not crash the implicit latest-run path."""
    bad_dir = temp_spawnd_dir / '.spawnd' / 'runs' / 'zzz-bad'
    _ = bad_dir.mkdir(parents=True)
    _ = (bad_dir / 'spawnd.db').write_text('')
    _ = os.utime(bad_dir, (2, 2))
    good = 'aaa-good'
    db = init_db(good)
    _ = insert_plan(db, good, 'good-plan', 'name: good')
    _ = db.close()
    _ = os.utime(temp_spawnd_dir / '.spawnd' / 'runs' / good, (1, 1))
    result = runner.invoke(main, ['status'])
    assert result.exit_code == 0
    assert 'Run: aaa-good' in result.output

def test_resume_command_rejects_stale_run_db(runner, temp_spawnd_dir):
    """resume should fail cleanly for empty or stale DB files."""
    stale_dir = temp_spawnd_dir / '.spawnd' / 'runs' / 'stale-run'
    _ = stale_dir.mkdir(parents=True)
    _ = (stale_dir / 'spawnd.db').write_text('')
    result = runner.invoke(main, ['resume', 'stale-run'])
    assert result.exit_code != 0
    assert 'Run not found: stale-run' in result.output

def test_run_resume_rejects_stale_run_db(runner, temp_spawnd_dir):
    """run --resume should use the same stale-run guard as resume."""
    stale_dir = temp_spawnd_dir / '.spawnd' / 'runs' / 'stale-run'
    _ = stale_dir.mkdir(parents=True)
    _ = (stale_dir / 'spawnd.db').write_text('')
    result = runner.invoke(main, ['run', '--resume', '--run-id', 'stale-run'])
    assert result.exit_code != 0
    assert 'Run not found: stale-run' in result.output

def test_cancel_command(runner, temp_spawnd_dir):
    """Test cancel command."""
    run_id = 'test-cancel'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test-plan", status="running")
    _ = insert_agent(db, run_id, 'agent1', 'Test prompt')
    _ = update_agent_status(db, run_id, 'agent1', 'running')
    _ = db.close()
    result = runner.invoke(main, ['cancel', run_id])
    assert result.exit_code == 0
    assert 'Cancelled run' in result.output
    assert 'Agents cancelled: 1' in result.output

def test_cancel_command_marks_pending_and_blocked_agents_cancelled(runner, temp_spawnd_dir):
    """Cancelling a run should not leave pending/blocked agents behind."""
    run_id = 'test-cancel-pending'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test-plan", status="running")
    _ = insert_agent(db, run_id, 'running_agent', 'Run now')
    _ = insert_agent(db, run_id, 'pending_agent', 'Queued')
    _ = insert_agent(db, run_id, 'blocked_agent', 'Waiting')
    _ = update_agent_status(db, run_id, 'running_agent', 'running')
    _ = update_agent_status(db, run_id, 'blocked_agent', 'blocked')
    _ = db.close()
    result = runner.invoke(main, ['cancel', run_id])
    assert result.exit_code == 0
    assert 'Agents cancelled: 3' in result.output
    db = init_db(run_id)
    statuses = {row['name']: row['status'] for row in db.execute('SELECT name, status FROM agents WHERE run_id = ?', (run_id,)).fetchall()}
    _ = db.close()
    assert statuses == {'running_agent': 'cancelled', 'pending_agent': 'cancelled', 'blocked_agent': 'cancelled'}

def test_cancel_nonexistent_run(runner, temp_spawnd_dir):
    """Test cancel command with nonexistent run."""
    result = runner.invoke(main, ['cancel', 'nonexistent'])
    assert result.exit_code != 0
    assert 'Run not found: nonexistent' in result.output

def test_logs_list(runner, temp_spawnd_dir):
    """Test logs command lists available logs."""
    run_id = 'test-logs'
    logs_dir = temp_spawnd_dir / '.spawnd' / 'runs' / run_id / 'logs'
    _ = logs_dir.mkdir(parents=True)
    _ = (logs_dir / 'agent1.log').write_text('test log content')
    result = runner.invoke(main, ['logs', run_id])
    assert result.exit_code == 0
    assert 'agent1' in result.output

def test_logs_agent(runner, temp_spawnd_dir):
    """Test logs command with specific agent."""
    run_id = 'test-logs-agent'
    logs_dir = temp_spawnd_dir / '.spawnd' / 'runs' / run_id / 'logs'
    _ = logs_dir.mkdir(parents=True)
    _ = (logs_dir / 'agent1.log').write_text('line1\nline2\nline3')
    result = runner.invoke(main, ['logs', run_id, '-a', 'agent1'])
    assert result.exit_code == 0
    assert 'line1' in result.output

def test_clean_specific_run(runner, temp_spawnd_dir):
    """Test clean command for specific run."""
    run_id = 'test-clean'
    run_dir = temp_spawnd_dir / '.spawnd' / 'runs' / run_id
    _ = run_dir.mkdir(parents=True)
    _ = (run_dir / 'spawnd.db').write_text('test')
    result = runner.invoke(main, ['clean', run_id])
    assert result.exit_code == 0
    assert 'Cleaned: test-clean' in result.output
    assert not run_dir.exists()

def test_clean_nonexistent_run(runner, temp_spawnd_dir):
    """Test clean command with nonexistent run."""
    result = runner.invoke(main, ['clean', 'nonexistent'])
    assert result.exit_code == 0
    assert 'Run not found' in result.output

def test_clean_requires_arg(runner, temp_spawnd_dir):
    """Test clean command requires run_id or --all."""
    result = runner.invoke(main, ['clean'])
    assert result.exit_code != 0
    assert 'Provide a run_id or use --all' in result.output

def test_db_list_runs(runner, temp_spawnd_dir):
    """Test db command lists runs."""
    for i in range(3):
        run_id = f'test-db-{i}'
        db = init_db(run_id)
        insert_test_plan(db, run_id, "plan", spec="name: plan", status="pending")
        _ = db.close()
    result = runner.invoke(main, ['db'])
    assert result.exit_code == 0
    assert 'Available runs' in result.output

def test_db_query(runner, temp_spawnd_dir):
    """Test db command with query."""
    run_id = 'test-db-query'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test-plan", status="pending")
    _ = insert_agent(db, run_id, 'agent1', 'Test prompt')
    _ = db.close()
    result = runner.invoke(main, ['db', run_id, 'SELECT name FROM agents'])
    assert result.exit_code == 0
    assert 'agent1' in result.output

def test_roles_list(runner):
    """Test roles command lists roles."""
    result = runner.invoke(main, ['roles'])
    assert result.exit_code == 0
    assert 'Available roles' in result.output
    assert 'implementer' in result.output

def test_roles_show_specific(runner):
    """Test roles command shows specific role."""
    result = runner.invoke(main, ['roles', 'implementer'])
    assert result.exit_code == 0
    assert 'Role: implementer' in result.output
    assert 'Description:' in result.output

def test_roles_nonexistent(runner):
    """Test roles command with nonexistent role."""
    result = runner.invoke(main, ['roles', 'nonexistent'])
    assert result.exit_code != 0
    assert 'Role not found' in result.output

def test_merge_no_completed_agents(runner, temp_spawnd_dir):
    """Test merge command with no completed agents."""
    run_id = 'test-merge'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test-plan", status="running")
    _ = insert_agent(db, run_id, 'agent1', 'Test prompt')
    _ = db.close()
    result = runner.invoke(main, ['merge', run_id])
    assert result.exit_code == 0
    assert 'No completed agents to merge' in result.output

def test_merge_dry_run(runner, temp_spawnd_dir):
    """Test merge command dry run."""
    run_id = 'test-merge-dry'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test-plan", status="completed")
    _ = insert_agent(db, run_id, 'agent1', 'Test prompt')
    _ = update_agent_status(db, run_id, 'agent1', 'completed')
    _ = db.close()
    result = runner.invoke(main, ['merge', run_id, '--dry-run'])
    assert result.exit_code == 0
    assert 'Merge order' in result.output
    assert 'dry run' in result.output

def test_merge_conflict_does_not_report_success(runner, temp_spawnd_dir, monkeypatch):
    """Test merge command stops on merge conflicts instead of reporting success."""
    run_id = 'test-merge-conflict'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test-plan", status="completed")
    _ = insert_agent(db, run_id, 'agent1', 'Test prompt')
    _ = update_agent_status(db, run_id, 'agent1', 'completed')
    _ = db.execute('UPDATE agents SET branch = ? WHERE run_id = ? AND name = ?', ('spawnd/test/agent1', run_id, 'agent1'))
    _ = db.commit()
    _ = db.close()
    _ = monkeypatch.setattr('spawnd.cli.merge_branch_to_current', lambda branch: False)
    result = runner.invoke(main, ['merge', run_id])
    assert result.exit_code != 0
    assert 'Merge conflict detected' in result.output
    assert 'Merged successfully' not in result.output

def test_clean_removes_git_worktrees_before_deleting_run(runner, temp_spawnd_dir, monkeypatch):
    """Test clean command unregisters git worktrees before deleting artifacts."""
    run_id = 'test-clean-git'
    run_dir = temp_spawnd_dir / '.spawnd' / 'runs' / run_id
    _ = run_dir.mkdir(parents=True)
    _ = (run_dir / 'spawnd.db').write_text('test')
    cleaned: list[str] = []
    _ = monkeypatch.setattr('spawnd.cli.cleanup_run_worktrees', lambda rid: cleaned.append(rid))
    result = runner.invoke(main, ['clean', run_id])
    assert result.exit_code == 0
    assert cleaned == [run_id]
