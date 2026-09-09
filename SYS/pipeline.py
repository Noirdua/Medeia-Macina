"""
Re-export module for backward compatibility.

@deprecated: Prefer direct imports from SYS.pipeline_state or SYS.pipeline_executor.
  - from SYS.pipeline_state import PipelineState, get_pipeline_state, ...
  - from SYS.pipeline_executor import PipelineExecutor

This module re-exports all names from both submodules so existing code
that imports from SYS.pipeline continues to work unchanged.
"""
from SYS.pipeline_state import __all__ as _state_all
from SYS.pipeline_state import *  # noqa: F401, F403
from SYS.pipeline_executor import PipelineExecutor  # noqa: F401
from SYS.models import PipelineStageContext  # noqa: F401

__all__ = list(_state_all) + ["PipelineExecutor", "PipelineStageContext"]
