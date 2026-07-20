"""
Environment validation for worktrees.

Validates .env configuration and checks for issues before starting containers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .docker_ops import is_docker_running
from .git_ops import get_main_repo_root
from .ports import check_port_conflicts, check_registry_conflicts
from .registry import read_registry


@dataclass
class ValidationResult:
    """Result of a validation check."""

    name: str
    passed: bool
    message: str
    is_error: bool = True  # False means warning


@dataclass
class ValidationReport:
    """Complete validation report."""

    results: list[ValidationResult] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationResult]:
        return [r for r in self.results if not r.passed and r.is_error]

    @property
    def warnings(self) -> list[ValidationResult]:
        return [r for r in self.results if not r.passed and not r.is_error]

    @property
    def passed(self) -> list[ValidationResult]:
        return [r for r in self.results if r.passed]

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    def add(self, result: ValidationResult) -> None:
        self.results.append(result)


def load_env_file(worktree_path: str) -> dict[str, str]:
    """Load environment variables from .env file."""
    env_path = Path(worktree_path) / '.env'
    env_vars: dict[str, str] = {}

    if not env_path.exists():
        return env_vars

    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, _, value = line.partition('=')
                # Remove quotes
                value = value.strip().strip('"').strip("'")
                env_vars[key.strip()] = value

    return env_vars


def validate_worktree(worktree_path: str, strict_ports: bool = True) -> ValidationReport:
    """
    Validate a worktree environment.

    Args:
        worktree_path: Path to the worktree to validate.
        strict_ports: If True, port conflicts are errors. If False, they're warnings.

    Returns:
        ValidationReport with all check results.
    """
    report = ValidationReport()
    path = Path(worktree_path)

    # Check 1: .env file exists
    env_path = path / '.env'
    if not env_path.exists():
        report.add(
            ValidationResult(
                name='.env file',
                passed=False,
                message='.env file not found. Run: cp .env.example .env',
                is_error=True,
            )
        )
        # Can't continue without .env
        return report
    else:
        report.add(ValidationResult(name='.env file', passed=True, message='.env file exists'))

    # Load environment variables
    env_vars = load_env_file(worktree_path)

    # Whether this project uses a shared dev-database server. When it does, the
    # per-worktree DB_PORT is vestigial and the meaningful value is SHARED_DB_PORT.
    from .config import get_project_config

    project_config = get_project_config(worktree_path)
    uses_shared_db = project_config.has_shared_db()
    uses_shared_redis = project_config.has_shared_redis()

    # Check 2: WEB_PORT is set
    web_port = env_vars.get('WEB_PORT')
    if not web_port:
        report.add(
            ValidationResult(
                name='WEB_PORT',
                passed=False,
                message='WEB_PORT not set in .env',
                is_error=True,
            )
        )
    else:
        report.add(ValidationResult(name='WEB_PORT', passed=True, message=f'WEB_PORT set: {web_port}'))

    # Check 3: database port. Shared-DB projects use a single global SHARED_DB_PORT
    # (identical across worktrees); non-shared projects use a per-worktree DB_PORT.
    db_port = env_vars.get('DB_PORT')
    if uses_shared_db:
        shared_port_env = project_config.get_shared_db_port_env()
        shared_port = env_vars.get(shared_port_env)
        if shared_port:
            report.add(
                ValidationResult(name=shared_port_env, passed=True, message=f'{shared_port_env} set: {shared_port}')
            )
        else:
            report.add(
                ValidationResult(
                    name=shared_port_env,
                    passed=False,
                    message=f'{shared_port_env} not set in .env (shared dev database)',
                    is_error=True,
                )
            )
    elif not db_port:
        report.add(
            ValidationResult(
                name='DB_PORT',
                passed=False,
                message='DB_PORT not set in .env',
                is_error=True,
            )
        )
    else:
        report.add(ValidationResult(name='DB_PORT', passed=True, message=f'DB_PORT set: {db_port}'))

    # Check 3b: Redis. Shared-Redis projects retire the per-worktree REDIS_PORT in
    # favour of a single global SHARED_REDIS_PORT plus per-worktree logical DB numbers;
    # non-shared projects still use a per-worktree REDIS_PORT.
    redis_port = env_vars.get('REDIS_PORT')
    if uses_shared_redis:
        shared_redis_env = project_config.get_shared_redis_port_env()
        shared_redis_port = env_vars.get(shared_redis_env)
        if shared_redis_port:
            report.add(
                ValidationResult(
                    name=shared_redis_env, passed=True, message=f'{shared_redis_env} set: {shared_redis_port}'
                )
            )
        else:
            report.add(
                ValidationResult(
                    name=shared_redis_env,
                    passed=False,
                    message=f'{shared_redis_env} not set in .env (shared dev Redis)',
                    is_error=True,
                )
            )
    elif not redis_port:
        report.add(
            ValidationResult(
                name='REDIS_PORT',
                passed=False,
                message='REDIS_PORT not set in .env',
                is_error=True,
            )
        )
    else:
        report.add(ValidationResult(name='REDIS_PORT', passed=True, message=f'REDIS_PORT set: {redis_port}'))

    # Check 4: Port conflicts (if ports are set)
    if web_port:
        try:
            web_port_int = int(web_port)
            # The per-worktree DB port is vestigial for shared-DB projects; passing None
            # skips it so the shared server on 5432 isn't reported as a false conflict.
            db_port_int = int(db_port) if (db_port and not uses_shared_db) else None
            # Shared-Redis projects have no per-worktree redis port; passing None skips
            # the check so the shared server's single port isn't a false conflict.
            redis_port_int = int(redis_port) if (redis_port and not uses_shared_redis) else None

            # Check for active port conflicts
            conflicts = check_port_conflicts(web_port_int, db_port_int, redis_port_int)
            for conflict in conflicts:
                proc_info = ''
                if conflict.process_name:
                    proc_info = f' (in use by {conflict.process_name}'
                    if conflict.process_pid:
                        proc_info += f', PID: {conflict.process_pid}'
                    proc_info += ')'
                report.add(
                    ValidationResult(
                        name=f'{conflict.port_type.upper()}_PORT conflict',
                        passed=False,
                        message=f'Port {conflict.port} is already in use{proc_info}',
                        is_error=strict_ports,
                    )
                )

            # Check for registry conflicts
            reg_conflicts = check_registry_conflicts(
                web_port_int, db_port_int, redis_port_int, exclude_path=worktree_path
            )
            for conflict in reg_conflicts:
                report.add(
                    ValidationResult(
                        name=f'{conflict.port_type.upper()}_PORT registry conflict',
                        passed=False,
                        message=f'Port {conflict.port} is assigned to {conflict.process_name}',
                        is_error=strict_ports,
                    )
                )

            if not conflicts and not reg_conflicts:
                report.add(
                    ValidationResult(name='Port availability', passed=True, message='No port conflicts detected')
                )

        except ValueError:
            report.add(
                ValidationResult(
                    name='Port format',
                    passed=False,
                    message='WEB_PORT, DB_PORT, or REDIS_PORT is not a valid integer',
                    is_error=True,
                )
            )

    # Check 5: Default ports warning
    if web_port == '58000':
        report.add(
            ValidationResult(
                name='Default WEB_PORT',
                passed=False,
                message='Using default WEB_PORT=58000. This may conflict with main worktree.',
                is_error=False,
            )
        )

    if db_port == '5432' and not uses_shared_db:
        report.add(
            ValidationResult(
                name='Default DB_PORT',
                passed=False,
                message='Using default DB_PORT=5432. This may conflict with main worktree.',
                is_error=False,
            )
        )

    # Check 6: Required environment variables
    required_vars = ['POSTGRES_HOST', 'POSTGRES_NAME', 'POSTGRES_USER', 'POSTGRES_PASSWORD']
    for var in required_vars:
        if var not in env_vars or not env_vars[var]:
            report.add(
                ValidationResult(
                    name=var,
                    passed=False,
                    message=f'{var} not set in .env',
                    is_error=True,
                )
            )

    # Check 7: Docker is running
    if is_docker_running():
        report.add(ValidationResult(name='Docker', passed=True, message='Docker is running'))
    else:
        report.add(
            ValidationResult(
                name='Docker',
                passed=False,
                message='Docker is not running',
                is_error=True,
            )
        )

    # Check 8: docker-compose.local.yml exists
    compose_file = path / 'docker-compose.local.yml'
    if compose_file.exists():
        report.add(
            ValidationResult(
                name='docker-compose.local.yml',
                passed=True,
                message='docker-compose.local.yml exists',
            )
        )
    else:
        report.add(
            ValidationResult(
                name='docker-compose.local.yml',
                passed=False,
                message='docker-compose.local.yml not found',
                is_error=True,
            )
        )

    # Check 9: Registry consistency
    registry = read_registry(str(get_main_repo_root()))
    if registry:
        entry = registry.find_by_path(worktree_path)
        if entry:
            report.add(
                ValidationResult(
                    name='Registry',
                    passed=True,
                    message='Worktree registered in registry',
                )
            )

            # Verify ports match
            if web_port and int(web_port) != entry.ports.web:
                report.add(
                    ValidationResult(
                        name='WEB_PORT mismatch',
                        passed=False,
                        message=f'.env: {web_port}, Registry: {entry.ports.web}',
                        is_error=False,
                    )
                )

            if db_port and not uses_shared_db and int(db_port) != entry.ports.db:
                report.add(
                    ValidationResult(
                        name='DB_PORT mismatch',
                        passed=False,
                        message=f'.env: {db_port}, Registry: {entry.ports.db}',
                        is_error=False,
                    )
                )
        else:
            report.add(
                ValidationResult(
                    name='Registry',
                    passed=False,
                    message="Worktree not registered. Was it created with 'worktree-manager create'?",
                    is_error=False,
                )
            )

    return report
