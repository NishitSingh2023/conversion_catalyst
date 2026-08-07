"""Guard the contract between Terraform and the Python codebase.

Terraform deploys each stage as a container image with an ``image_config.command``
naming a dotted handler path. Nothing in ``terraform validate``, ``plan`` or
``apply`` checks that the path resolves - a function whose module is missing
deploys cleanly and fails at invocation with Runtime.ImportModuleError, several
states into a run that has already opened a pipeline_runs row.

These tests close that gap by parsing the handler paths straight out of the
Terraform config and importing each one.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAMBDAS_TF = REPO_ROOT / "terraform" / "lambdas.tf"

# Stages declared in Terraform whose handler is not written yet. Each entry is a
# deliberate acknowledgement that the stack cannot be deployed end to end until
# it is implemented. Remove an entry when its handler lands - the test will then
# enforce it forever.
PENDING_HANDLERS = {
    "lambdas.scoring.handler.lambda_handler",
    "lambdas.optimizer.handler.lambda_handler",
    "lambdas.pool.handler.lambda_handler",
    "lambdas.lsq_push.handler.lambda_handler",
}


def terraform_handlers() -> list[str]:
    """Every handler path Terraform will deploy."""
    content = LAMBDAS_TF.read_text()
    return re.findall(r'handler\s*=\s*"([^"]+)"', content)


def test_terraform_declares_handlers():
    handlers = terraform_handlers()
    assert handlers, "no handler paths found in terraform/lambdas.tf"
    # Every handler must be a dotted path ending in the callable name.
    for h in handlers:
        assert h.endswith(".lambda_handler"), f"unexpected handler shape: {h}"


@pytest.mark.parametrize("handler", sorted(set(terraform_handlers()) - PENDING_HANDLERS))
def test_declared_handler_is_importable(handler):
    """A handler Terraform deploys must exist and be callable."""
    module_path, func_name = handler.rsplit(".", 1)
    module = importlib.import_module(module_path)
    assert callable(getattr(module, func_name)), f"{handler} is not callable"


def test_pending_handlers_are_still_genuinely_missing():
    """Once a pending stage is implemented, remove it from PENDING_HANDLERS.

    This keeps the allowlist honest instead of letting it mask a regression.
    """
    resolved = []
    for handler in sorted(PENDING_HANDLERS):
        module_path, func_name = handler.rsplit(".", 1)
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError:
            continue
        if callable(getattr(module, func_name, None)):
            resolved.append(handler)
    assert not resolved, (
        "These handlers now exist - remove them from PENDING_HANDLERS so the "
        f"import test enforces them: {resolved}"
    )


def test_no_reserved_lambda_env_vars():
    """Lambda rejects any function configuration that sets a reserved key.

    AWS_REGION was previously set in the shared env map, which fails
    CreateFunction for every function in the stack.
    """
    reserved = {
        "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_EXECUTION_ENV",
        "_HANDLER", "LAMBDA_TASK_ROOT", "LAMBDA_RUNTIME_DIR",
    }
    main_tf = (REPO_ROOT / "terraform" / "main.tf").read_text()
    env_block = main_tf.split("lambda_env = {", 1)[1].split("}", 1)[0]
    declared = set(re.findall(r"^\s*([A-Z_][A-Z0-9_]*)\s*=", env_block, re.MULTILINE))
    assert not (declared & reserved), f"reserved Lambda env vars set: {declared & reserved}"
