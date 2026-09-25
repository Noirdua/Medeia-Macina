"""Public pipeline facade.

Callers should use ``from SYS import pipeline as ctx``. The implementation
lives in pipeline_state and pipeline_executor; this module is the import
surface, not a second runtime.
"""
from SYS.pipeline_state import __all__ as _state_all
from SYS.pipeline_state import *  # noqa: F401, F403
from SYS.pipeline_executor import PipelineExecutor  # noqa: F401
from SYS.models import PipelineStageContext  # noqa: F401

__all__ = list(_state_all) + ["PipelineExecutor", "PipelineStageContext"]
